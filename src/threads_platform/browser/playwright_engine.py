from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ProxySettings,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from threads_platform.browser.contracts import (
    BROWSER_FEED_CANDIDATE_BOUND,
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserContractError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserRuntimeUnavailable,
    NavigationTimeout,
    RemoteSessionStateUncertain,
    UnsupportedUIState,
)
from threads_platform.browser.threads_job_ui_reader import (
    AsyncThreadsJobUiReader,
    UiFeedBatch,
    UiPageState,
    UiPostDetail,
    UiPostTarget,
)
from threads_platform.browser.ui_diagnostic import (
    DIAGNOSTIC_SCRIPT,
    UiDiagnosticSchemaError,
    UiDiagnosticSnapshot,
    validate_ui_diagnostic_snapshot,
)

_MAX_SYNTHETIC_FIXTURE_PAGES = 128
_MAX_SYNTHETIC_FIXTURE_HTML_CODEPOINTS = 1_100_000
_MANUAL_ASSIST_CLICK_TIMEOUT_SECONDS = 20

_FEED_SCAN_SCRIPT = r"""
({allowedOrigin, candidateBound}) => {
  if (window.location.origin !== allowedOrigin) {
    return {ok: false, reason: 'origin_mismatch'};
  }
  const anchors = document.querySelectorAll('a[href*="/post/"]');
  const permalinks = [];
  for (const anchor of anchors) {
    const href = anchor.getAttribute('href');
    if (typeof href !== 'string') continue;
    try {
      const url = new URL(href, window.location.href);
      if (url.origin !== allowedOrigin) continue;
      if (!/^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/.test(url.pathname)) {
        continue;
      }
      permalinks.push(url.pathname);
      if (permalinks.length > candidateBound) {
        return {ok: false, reason: 'candidate_bound_exceeded'};
      }
    } catch (_) {
      continue;
    }
  }
  return {ok: true, permalinks};
}
"""


_THREAD_OPEN_SCRIPT = r"""
({allowedOrigin, targetRef, targetAuthor}) => {
  const normalizePath = (path) => path.endsWith('/') ? path.slice(0, -1) : path;
  const postPathPattern = /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}$/;
  const profilePathPattern = /^\/@[A-Za-z0-9._]{1,30}$/;
  const postLink = (href) => {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, allowedOrigin);
      if (!url.pathname.includes('/post/')) return null;
      if (url.origin !== allowedOrigin || url.search || url.hash) {
        return {path: null};
      }
      const path = normalizePath(url.pathname);
      return postPathPattern.test(path) ? {path} : {path: null};
    } catch (_) {
      return href.includes('/post/') ? {path: null} : null;
    }
  };
  const semanticPath = (href) => {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, allowedOrigin);
      if (url.origin !== allowedOrigin || url.search || url.hash) return null;
      return normalizePath(url.pathname);
    } catch (_) {
      return null;
    }
  };
  if (window.location.origin !== allowedOrigin) return {outcome: 'uncertain'};
  if (normalizePath(window.location.pathname) !== targetRef) return {outcome: 'uncertain'};

  const anchors = Array.from(document.querySelectorAll('a[href]'));
  if (anchors.length > 1000) return {outcome: 'invalid'};
  const mainLandmarks = Array.from(document.querySelectorAll('main, [role="main"]'));
  if (mainLandmarks.length !== 1) return {outcome: 'uncertain'};
  const main = mainLandmarks[0];
  const cardSelector = 'article, [role="article"]';
  const cards = Array.from(main.querySelectorAll(cardSelector));
  if (cards.length > 1000) return {outcome: 'invalid'};
  const isInAuxiliaryRegion = (card) => {
    let current = card.parentElement;
    while (current && current !== main) {
      if (current.matches('aside, nav, footer')) return true;
      current = current.parentElement;
    }
    return current !== main;
  };
  const rootCards = cards.filter((card) => {
    const parent = card.parentElement;
    return (!parent || !parent.closest(cardSelector)) && !isInAuxiliaryRegion(card);
  });
  const owns = (element, card) => element.closest(cardSelector) === card;
  const targetPermalinkOnPage = anchors.some(
    (anchor) => semanticPath(anchor.getAttribute('href')) === targetRef
  );
  const targetCards = [];
  for (const card of rootCards) {
    const ownAnchors = anchors.filter((anchor) => owns(anchor, card));
    const postPaths = new Set();
    let hasInvalidPostLink = false;
    for (const anchor of ownAnchors) {
      const result = postLink(anchor.getAttribute('href'));
      if (result === null) continue;
      if (result.path === null) {
        hasInvalidPostLink = true;
      } else {
        postPaths.add(result.path);
      }
    }
    if (postPaths.has(targetRef)) {
      targetCards.push({card, postPaths, hasInvalidPostLink});
    }
  }
  if (targetCards.length === 0) {
    return targetPermalinkOnPage
      ? {outcome: 'uncertain'}
      : {outcome: 'missing_permalink'};
  }
  if (targetCards.length !== 1) return {outcome: 'invalid'};
  const candidate = targetCards[0];
  if (candidate.hasInvalidPostLink || candidate.postPaths.size !== 1) {
    return {outcome: 'invalid'};
  }

  const authorPath = `/@${targetAuthor}`;
  const authorPaths = new Set();
  const headers = Array.from(candidate.card.querySelectorAll('header'));
  for (const header of headers) {
    if (!owns(header, candidate.card) || header.closest('blockquote')) continue;
    const sectioningOwner = header.closest('article, section, aside, nav');
    if (sectioningOwner !== null && sectioningOwner !== candidate.card) continue;
    if (candidate.card.matches('article') && sectioningOwner !== candidate.card) continue;
    for (const anchor of header.querySelectorAll('a[href]')) {
      if (!owns(anchor, candidate.card)) continue;
      const path = semanticPath(anchor.getAttribute('href'));
      if (path !== null && profilePathPattern.test(path)) authorPaths.add(path);
    }
  }
  if (authorPaths.size > 1) return {outcome: 'invalid'};
  if (!authorPaths.has(authorPath)) return {outcome: 'missing_author'};
  return {outcome: 'recognized'};
}
"""


_OPERATOR_CLICK_OBSERVER_SCRIPT = r"""
({allowedOrigin, sourceRoute, bindingName}) => {
  const stateKey = '__threadsLocalUiDiagnosticClickState';
  if (window[stateKey] && window[stateKey].installed === true) return true;
  const state = {installed: true, armed: true, expectedOrigin: allowedOrigin, sourceRoute};
  const bucket = (count) => count === 0 ? 'ZERO' : count === 1 ? 'ONE' : 'MANY';
  const normalize = (value) => typeof value === 'string'
    ? value.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase() : '';
  const safeTag = (element) => [
    'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'NAV', 'MAIN', 'ARTICLE', 'HEADER',
    'SECTION', 'ASIDE', 'DIV', 'FORM'
  ].includes(element.tagName) ? element.tagName : 'OTHER';
  const safeRole = (element) => {
    const role = (element.getAttribute('role') || '').trim().toLowerCase();
    if (role === 'button') return 'BUTTON';
    if (role === 'link') return 'LINK';
    if (element.tagName === 'BUTTON') return 'BUTTON';
    if (element.tagName === 'A') return 'LINK';
    return 'OTHER';
  };
  const safeLabel = (element) => {
    const name = (element.getAttribute('aria-label') || element.getAttribute('title') || '')
      .replace(/\s+/g, ' ').trim().toLowerCase();
    if (name === 'search' || name === 'search and explore') return 'SEARCH';
    if (['profile', 'your profile', 'my profile', 'account', 'your account'].includes(name)) {
      return 'PROFILE';
    }
    if (name === 'home') return 'HOME';
    if (name === 'for you') return 'FOR_YOU';
    if (name === 'top') return 'TOP';
    if (name === 'recent') return 'RECENT';
    return 'OTHER';
  };
  const roleOf = (element) => {
    const role = normalize(element.getAttribute('role'));
    if (role === 'button' || element.tagName === 'BUTTON') return 'BUTTON';
    if (role === 'link' || element.tagName === 'A') return 'LINK';
    return 'OTHER';
  };
  const visible = (element) => {
    let current = element;
    for (let depth = 0; current && depth < 32; current = current.parentElement, depth += 1) {
      if (current.hidden || current.getAttribute('aria-hidden') === 'true') return false;
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
          current.hasAttribute('inert') || Number.parseFloat(style.opacity) === 0) return false;
    }
    if (current !== null) return false;
    const rects = Array.from(element.getClientRects());
    return rects.some((rect) => rect.width > 0 && rect.height > 0 && rect.right > 0 &&
      rect.left < window.innerWidth && rect.bottom > 0 && rect.top < window.innerHeight);
  };
  const routeClass = () => {
    if (window.location.origin !== state.expectedOrigin) return 'OTHER';
    const path = window.location.pathname.replace(/\/$/, '') || '/';
    if (/^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}$/i.test(path)) return 'POST';
    if (/\/(search|explore)(\/|$)/i.test(path) || new URL(window.location.href).searchParams.has('q')) {
      return 'SEARCH';
    }
    if (path === '/' || /^\/(home|for_you)$/i.test(path)) return 'HOME';
    return 'OTHER';
  };
  const visibleSearchboxes = () => Array.from(document.querySelectorAll(
    'input,textarea,[role="searchbox"],[role="textbox"]'
  )).filter((element) => {
    if (!visible(element)) return false;
    const role = normalize(element.getAttribute('role'));
    const isTextEntry = ['INPUT', 'TEXTAREA'].includes(element.tagName);
    return role === 'searchbox' || isTextEntry &&
      (safeLabel(element) === 'SEARCH' || normalize(element.getAttribute('type')) === 'search');
  });
  window.addEventListener('click', (event) => {
    if (!state.armed || !event.isTrusted || routeClass() !== state.sourceRoute) return;
    const control = event.composedPath().slice(0, 32).find((item) => item instanceof Element &&
      item.matches('a[href],button,[role="button"],[role="link"]'));
    if (!(control instanceof Element) || !visible(control)) return;
    const navigationCandidates = Array.from(document.querySelectorAll('nav,[role="navigation"]'));
    const controlCandidates = Array.from(document.querySelectorAll(
      'a[href],button,[role="button"],[role="link"]'
    ));
    const searchboxCandidates = visibleSearchboxes();
    if (navigationCandidates.length > 3_000 || controlCandidates.length > 3_000 ||
        searchboxCandidates.length > 3_000) return;
    state.armed = false;
    const navigation = navigationCandidates.filter(visible);
    const label = safeLabel(control);
    const role = safeRole(control);
    const tag = safeTag(control);
    const semanticMatches = label === 'OTHER' ? [] : controlCandidates.filter((candidate) =>
      visible(candidate) && safeRole(candidate) === role && safeLabel(candidate) === label
    );
    const anchor = control.closest('a[href]');
    let targetPath = null;
    if (anchor) {
      try {
        const target = new URL(anchor.getAttribute('href'), window.location.href);
        const path = target.pathname.replace(/\/$/, '');
        if (target.origin === state.expectedOrigin && !target.username && !target.password &&
            !target.search && !target.hash &&
            /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}$/.test(path)) {
          targetPath = path;
        }
      } catch (_) { /* Keep malformed destinations out of the diagnostic. */ }
    }
    const report = {
      event_is_trusted: true,
      source_route: routeClass(),
      role,
      tag,
      label,
      within_unique_navigation: navigation.length === 1 && navigation[0].contains(control),
      stable_semantic_locator_unique: semanticMatches.length === 1,
      before_searchboxes: bucket(searchboxCandidates.length),
      is_post_permalink: targetPath !== null,
      target_path: targetPath
    };
    try {
      window[bindingName](JSON.stringify(report));
    } catch (_) { /* A lost CDP binding leaves the action unverified. */ }
  }, true);
  window[stateKey] = state;
  return true;
}
"""

_UI_DIAGNOSTIC_CDP_WORLD_PREFIX = "threadsLocalUiDiagnostic"
_UI_DIAGNOSTIC_CDP_BINDING_PREFIX = "threadsLocalUiDiagnosticRecord"

_BROWSER_READ_READINESS_TIMEOUT_SECONDS = 2.0
_BROWSER_READ_READINESS_POLL_SECONDS = 0.05

_PROFILE_OPEN_SCRIPT = r"""
({allowedOrigin, targetRef, ancestorBound}) => {
  const normalizePath = (path) => path.endsWith('/') ? path.slice(0, -1) : path;
  const anchorPath = (href) => {
    if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//') ||
        href.includes('?') || href.includes('#')) {
      return null;
    }
    return normalizePath(href);
  };
  const isPostPermalink = (href) => {
    if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//')) {
      return false;
    }
    const pathname = href.split(/[?#]/, 1)[0];
    return /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/.test(pathname);
  };
  if (window.location.origin !== allowedOrigin) return {outcome: 'uncertain'};
  if (normalizePath(window.location.pathname) !== targetRef) return {outcome: 'uncertain'};

  const anchors = Array.from(document.querySelectorAll('a[href]'));
  if (anchors.length > 1000) return {outcome: 'invalid'};
  const headings = document.querySelectorAll('h1');
  const headingBound = 8;
  if (headings.length === 0 || headings.length > headingBound) return {outcome: 'invalid'};
  const exactTargetAnchors = anchors.filter(
    (anchor) => anchorPath(anchor.getAttribute('href')) === targetRef
  );
  if (exactTargetAnchors.length === 0) return {outcome: 'uncertain'};

  const qualifyingHeaders = new Set();
  for (let headingIndex = 0; headingIndex < headings.length; headingIndex += 1) {
    const heading = headings[headingIndex];
    if (!heading || !(heading.textContent || '').replace(/\s+/g, ' ').trim()) continue;

    let node = heading.parentElement;
    let profileHeader = null;
    for (let depth = 1; node && depth <= ancestorBound; depth += 1) {
      if (node.tagName === 'DIV') {
        profileHeader = node;
        break;
      }
      node = node.parentElement;
    }
    if (profileHeader === null) continue;

    const headerLinks = anchors.filter((link) => profileHeader.contains(link));
    if (headerLinks.some((link) => isPostPermalink(link.getAttribute('href')))) continue;
    if (headerLinks.some((link) => anchorPath(link.getAttribute('href')) === targetRef)) {
      qualifyingHeaders.add(profileHeader);
    }
  }
  if (qualifyingHeaders.size !== 1) return {outcome: 'invalid'};
  return {outcome: 'recognized'};
}
"""


class PlaywrightBrowserEngine:
    """Pinned Playwright/Chromium lifecycle for local browser profiles."""

    def __init__(self, *, navigation_timeout_ms: int = 30_000) -> None:
        if navigation_timeout_ms < 1:
            raise ValueError("navigation timeout must be positive")
        self._navigation_timeout_ms = navigation_timeout_ms

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        profile_directory = request.profile_directory.resolve(strict=True)
        if not profile_directory.is_dir():
            raise BrowserContractError()
        proxy: ProxySettings | None = playwright_proxy_settings(request)
        manager = async_playwright()
        try:
            playwright = await manager.start()
        except PlaywrightError:
            raise BrowserRuntimeUnavailable("BROWSER_RUNTIME_UNAVAILABLE") from None
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_BINARY_MISSING")
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_directory),
                headless=request.headless,
                proxy=proxy,
                accept_downloads=False,
                # The route guard must see every top-level navigation request.
                service_workers="block",
            )
        except PlaywrightError:
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_START_FAILED") from None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
        except PlaywrightError:
            await context.close()
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_PAGE_CREATION_FAILED") from None
        return _PlaywrightBrowserSession(
            playwright,
            context,
            page,
            navigation_timeout_ms=self._navigation_timeout_ms,
        )


class _PlaywrightBrowserSession:
    def __init__(
        self,
        playwright: Playwright,
        context: BrowserContext,
        page: Page,
        *,
        navigation_timeout_ms: int,
    ) -> None:
        self._playwright = playwright
        self._context = context
        self._page = page
        self._browser: Browser | None = context.browser
        self._navigation_timeout_ms = navigation_timeout_ms
        self._crashed = False
        self._closed = False
        self._last_feed_permalinks: frozenset[str] = frozenset()
        self._allowed_navigation_origins: frozenset[str] = frozenset()
        self._navigation_guard_installed = False
        self._remote_state_uncertain = False
        self._synthetic_fixture_pages: dict[str, str] | None = None
        self._synthetic_fixture_missing = False
        self._synthetic_blocked_request_count = 0
        self._async_ui_reader: AsyncThreadsJobUiReader | None = None
        self._async_ui_reader_origin: str | None = None
        self._ui_diagnostic_cdp_session: Any = None
        self._ui_diagnostic_cdp_sequence = 0
        self._ui_diagnostic_cdp_context_frames: dict[int, str] = {}
        self._ui_diagnostic_click_armed_route: str | None = None
        self._ui_diagnostic_click_context_id: int | None = None
        self._ui_diagnostic_click_binding_name: str | None = None
        self._ui_diagnostic_click_main_frame_id: str | None = None
        self._ui_diagnostic_action_status = "UNVERIFIED"
        self._ui_diagnostic_operator_click: dict[str, object] | None = None
        # Volatile comparison input only; it is never copied to a report or log.
        self._ui_diagnostic_operator_click_path: str | None = None
        page.on("crash", self._on_page_crash)
        context.on("close", self._on_context_close)

    @property
    def synthetic_blocked_request_count(self) -> int:
        return self._synthetic_blocked_request_count

    @property
    def ui_scroll_count(self) -> int:
        return 0 if self._async_ui_reader is None else self._async_ui_reader.scroll_count

    async def install_synthetic_fixture_pages(
        self,
        pages: Mapping[str, str],
        *,
        origin: str,
    ) -> None:
        """Route a reserved local fixture map through this persistent profile session."""

        self._ensure_alive()
        normalized_origin = _normalized_browser_origin(origin)
        if (
            self._navigation_guard_installed
            or self._synthetic_fixture_pages is not None
            or normalized_origin != "https://threads.example.invalid"
            or not pages
            or len(pages) > _MAX_SYNTHETIC_FIXTURE_PAGES
        ):
            raise UnsupportedUIState()
        validated: dict[str, str] = {}
        for url, html in pages.items():
            parsed_origin = _normalized_browser_origin(url)
            if (
                parsed_origin != normalized_origin
                or not html
                or len(html) > _MAX_SYNTHETIC_FIXTURE_HTML_CODEPOINTS
                or url in validated
            ):
                raise UnsupportedUIState()
            validated[url] = html
        if f"{normalized_origin}/" not in validated:
            raise UnsupportedUIState()
        self._synthetic_fixture_pages = validated
        self._async_ui_reader_origin = normalized_origin

    async def verify_ui_account(self, expected_actor: str, *, origin: str) -> UiPageState:
        self._ensure_alive()
        try:
            state = await self._get_async_ui_reader(origin).verify_signed_in(expected_actor)
        except BrowserAdapterError:
            self._ensure_alive()
            await self._raise_ui_policy_error()
            raise
        self._ensure_alive()
        await self._raise_ui_policy_error()
        return state

    async def search_ui_feed(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
        one_post: bool = False,
    ) -> tuple[UiFeedBatch, ...]:
        self._ensure_alive()
        try:
            result = await self._get_async_ui_reader(origin).search(
                query,
                expected_actor=expected_actor,
                one_post=one_post,
            )
        except BrowserAdapterError:
            self._ensure_alive()
            await self._raise_ui_policy_error()
            raise
        self._ensure_alive()
        await self._raise_ui_policy_error()
        return result

    async def read_exact_ui_post(
        self,
        target: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> UiPostDetail:
        self._ensure_alive()
        try:
            result = await self._get_async_ui_reader(origin).read_exact_post(
                target,
                expected_actor=expected_actor,
            )
        except BrowserAdapterError:
            self._ensure_alive()
            await self._raise_ui_policy_error()
            raise
        self._ensure_alive()
        await self._raise_ui_policy_error()
        return result

    async def assisted_search_ui_results(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> tuple[UiPostTarget, ...]:
        self._ensure_alive()
        try:
            result = await self._get_async_ui_reader(origin).assisted_search_results(
                query,
                expected_actor=expected_actor,
            )
        except BrowserAdapterError:
            self._ensure_alive()
            await self._raise_ui_policy_error()
            raise
        self._ensure_alive()
        await self._raise_ui_policy_error()
        return result

    async def wait_for_assisted_post_click(
        self,
        candidates: tuple[UiPostTarget, ...],
        *,
        origin: str,
    ) -> str:
        """Bind one trusted Search-result click to the currently opened same-origin post."""

        self._ensure_alive()
        normalized_origin = _normalized_browser_origin(origin)
        if (
            normalized_origin not in {BROWSER_FEED_ORIGIN, "https://threads.example.invalid"}
            or self._allowed_navigation_origins != frozenset({normalized_origin})
            or not candidates
        ):
            raise UnsupportedUIState()
        paths: dict[str, str] = {}
        for candidate in candidates:
            parsed = urlsplit(candidate.canonical_post_url)
            if (
                _normalized_browser_origin(candidate.canonical_post_url) != normalized_origin
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(
                    r"/@[A-Za-z0-9._:-]{1,96}/(?:post-[A-Za-z0-9._:-]{1,128}|post/[A-Za-z0-9._:-]{1,128})",
                    parsed.path,
                )
            ):
                raise BrowserAdapterError("SEARCH_RESULTS_UNPROVEN")
            paths[parsed.path] = candidate.canonical_post_url
        if len(paths) != len(candidates):
            raise BrowserAdapterError("SEARCH_RESULTS_UNPROVEN")

        if (
            self._ui_diagnostic_click_armed_route != "SEARCH"
            and self._ui_diagnostic_operator_click is None
        ):
            raise UnsupportedUIState()
        deadline = asyncio.get_running_loop().time() + _MANUAL_ASSIST_CLICK_TIMEOUT_SECONDS
        while True:
            self._ensure_alive()
            await self._raise_navigation_policy_error()
            click = self._ui_diagnostic_operator_click
            clicked_path = self._ui_diagnostic_operator_click_path
            if click is not None:
                if click.get("source_route") != "SEARCH":
                    raise BrowserAdapterError("DETAIL_SELECTION_NOT_IN_RESULTS")
                if click.get("is_post_permalink") is not True:
                    raise BrowserAdapterError("DETAIL_SELECTION_UNVERIFIED")
                if clicked_path not in paths:
                    raise BrowserAdapterError("DETAIL_SELECTION_NOT_IN_RESULTS")
                try:
                    current = urlsplit(self._page.url)
                except ValueError:
                    raise BrowserAdapterError("DETAIL_PROVENANCE_UNVERIFIED") from None
                if (
                    _normalized_browser_origin(self._page.url) != normalized_origin
                    or current.path != clicked_path
                    or current.query
                    or current.fragment
                ):
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise BrowserAdapterError("DETAIL_PROVENANCE_UNVERIFIED")
                else:
                    return paths[clicked_path]
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserAdapterError("DETAIL_SELECTION_UNVERIFIED")
            await asyncio.sleep(min(0.1, remaining))

    async def read_current_ui_post(
        self,
        target: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> UiPostDetail:
        self._ensure_alive()
        try:
            result = await self._get_async_ui_reader(origin).read_current_exact_post(
                target,
                expected_actor=expected_actor,
            )
        except BrowserAdapterError:
            self._ensure_alive()
            await self._raise_ui_policy_error()
            raise
        self._ensure_alive()
        await self._raise_ui_policy_error()
        return result

    async def _ensure_ui_diagnostic_cdp(self) -> Any:
        if self._ui_diagnostic_cdp_session is not None:
            return self._ui_diagnostic_cdp_session
        session = cast(Any, await self._context.new_cdp_session(self._page))
        try:
            await session.send("Page.enable")
            await session.send("Runtime.enable")
        except PlaywrightError:
            try:
                await session.detach()
            except PlaywrightError:
                pass
            raise
        session.on("Runtime.bindingCalled", self._receive_ui_diagnostic_operator_click)
        self._ui_diagnostic_cdp_session = session
        return session

    async def _create_ui_diagnostic_world(self) -> tuple[Any, int, str]:
        session = await self._ensure_ui_diagnostic_cdp()
        frame_reply = cast(dict[str, object], await session.send("Page.getFrameTree"))
        raw_frame_tree = frame_reply.get("frameTree")
        if not isinstance(raw_frame_tree, dict):
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        frame_tree = cast(dict[str, object], raw_frame_tree)
        frame = frame_tree.get("frame")
        if not isinstance(frame, dict):
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        frame_dict = cast(dict[str, object], frame)
        frame_id = frame_dict.get("id")
        frame_url = frame_dict.get("url")
        if (
            not isinstance(frame_id, str)
            or not isinstance(frame_url, str)
            or _normalized_browser_origin(frame_url)
            not in {
                BROWSER_FEED_ORIGIN,
                "https://threads.example.invalid",
            }
        ):
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        self._ui_diagnostic_cdp_sequence += 1
        world_name = f"{_UI_DIAGNOSTIC_CDP_WORLD_PREFIX}{self._ui_diagnostic_cdp_sequence}"
        world_reply = cast(
            dict[str, object],
            await session.send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": world_name},
            ),
        )
        context_id = world_reply.get("executionContextId")
        if type(context_id) is not int:
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        return session, context_id, frame_id

    @staticmethod
    async def _evaluate_ui_diagnostic_world(
        session: Any,
        context_id: int,
        script: str,
        parameters: Mapping[str, object],
    ) -> object:
        expression = f"({script})({json.dumps(parameters, separators=(',', ':'))})"
        reply = cast(
            dict[str, object],
            await session.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context_id,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            ),
        )
        if "exceptionDetails" in reply:
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        raw_result = reply.get("result")
        if not isinstance(raw_result, dict) or "value" not in raw_result:
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        result = cast(dict[str, object], raw_result)
        return result["value"]

    async def inspect_ui_diagnostic(
        self,
        *,
        expected_username: str,
        configured_topic: str,
    ) -> UiDiagnosticSnapshot:
        """Return only the fixed, privacy-validated semantic graph for the current page."""

        self._ensure_alive()
        origin = self._async_ui_reader_origin
        if origin is None and len(self._allowed_navigation_origins) == 1:
            origin = next(iter(self._allowed_navigation_origins))
        if origin not in {
            BROWSER_FEED_ORIGIN,
            "https://threads.example.invalid",
        } or self._allowed_navigation_origins != frozenset({origin}):
            raise UnsupportedUIState()
        await self._raise_navigation_policy_error()
        try:
            operator_click = self._ui_diagnostic_operator_click
            cdp_session, context_id, _ = await self._create_ui_diagnostic_world()
            raw_snapshot = await asyncio.wait_for(
                self._evaluate_ui_diagnostic_world(
                    cdp_session,
                    context_id,
                    DIAGNOSTIC_SCRIPT,
                    {
                        "allowedOrigin": origin,
                        "expectedUsername": expected_username,
                        "configuredTopic": configured_topic,
                        "maxCandidates": 3_000,
                        "maxNodes": 180,
                        "maxRelations": 480,
                        "operatorClick": operator_click,
                        "operatorClickPath": self._ui_diagnostic_operator_click_path,
                        "operatorActionStatus": self._ui_diagnostic_action_status,
                    },
                ),
                timeout=3.0,
            )
        except TimeoutError, PlaywrightError:
            self._ensure_alive()
            await self._raise_navigation_policy_error()
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE") from None
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        try:
            return validate_ui_diagnostic_snapshot(raw_snapshot)
        except UiDiagnosticSchemaError:
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_SCHEMA_INVALID") from None

    async def arm_ui_diagnostic_operator_click(self, *, source_route: str) -> None:
        """Observe one trusted main-frame click in a CDP isolated world."""

        if source_route not in {"HOME", "SEARCH", "POST", "OTHER"}:
            raise UnsupportedUIState()
        self._ensure_alive()
        origin = self._async_ui_reader_origin
        if origin is None and len(self._allowed_navigation_origins) == 1:
            origin = next(iter(self._allowed_navigation_origins))
        if origin not in {
            BROWSER_FEED_ORIGIN,
            "https://threads.example.invalid",
        } or self._allowed_navigation_origins != frozenset({origin}):
            raise UnsupportedUIState()
        await self._raise_navigation_policy_error()
        if self._ui_diagnostic_click_context_id is not None:
            self._ui_diagnostic_cdp_context_frames.pop(self._ui_diagnostic_click_context_id, None)
        self._ui_diagnostic_operator_click = None
        self._ui_diagnostic_operator_click_path = None
        self._ui_diagnostic_click_armed_route = source_route
        self._ui_diagnostic_action_status = "UNVERIFIED"
        context_id: int | None = None
        try:
            cdp_session, context_id, frame_id = await self._create_ui_diagnostic_world()
            binding_name = f"{_UI_DIAGNOSTIC_CDP_BINDING_PREFIX}{self._ui_diagnostic_cdp_sequence}"
            # The context ID was created from Page.getFrameTree's main-frame ID;
            # the site and child frames have no access to this isolated-world binding.
            await cdp_session.send(
                "Runtime.addBinding",
                {"name": binding_name, "executionContextId": context_id},
            )
            self._ui_diagnostic_click_context_id = context_id
            self._ui_diagnostic_click_binding_name = binding_name
            self._ui_diagnostic_click_main_frame_id = frame_id
            self._ui_diagnostic_cdp_context_frames[context_id] = frame_id
            armed = await self._evaluate_ui_diagnostic_world(
                cdp_session,
                context_id,
                _OPERATOR_CLICK_OBSERVER_SCRIPT,
                {
                    "allowedOrigin": origin,
                    "sourceRoute": source_route,
                    "bindingName": binding_name,
                },
            )
        except PlaywrightError:
            self._ensure_alive()
            await self._raise_navigation_policy_error()
            self._ui_diagnostic_click_armed_route = None
            self._ui_diagnostic_click_context_id = None
            self._ui_diagnostic_click_binding_name = None
            self._ui_diagnostic_click_main_frame_id = None
            if context_id is not None:
                self._ui_diagnostic_cdp_context_frames.pop(context_id, None)
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE") from None
        except BrowserRuntimeUnavailable:
            self._ui_diagnostic_click_armed_route = None
            self._ui_diagnostic_click_context_id = None
            self._ui_diagnostic_click_binding_name = None
            self._ui_diagnostic_click_main_frame_id = None
            if context_id is not None:
                self._ui_diagnostic_cdp_context_frames.pop(context_id, None)
            raise
        if armed is not True:
            self._ui_diagnostic_click_armed_route = None
            self._ui_diagnostic_click_context_id = None
            self._ui_diagnostic_click_binding_name = None
            self._ui_diagnostic_click_main_frame_id = None
            self._ui_diagnostic_cdp_context_frames.pop(context_id, None)
            raise BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE")
        await self._raise_navigation_policy_error()

    def _receive_ui_diagnostic_operator_click(self, event: object) -> None:
        expected_route = self._ui_diagnostic_click_armed_route
        expected_context_id = self._ui_diagnostic_click_context_id
        expected_binding_name = self._ui_diagnostic_click_binding_name
        expected_frame_id = self._ui_diagnostic_click_main_frame_id
        if (
            expected_route is None
            or self._ui_diagnostic_operator_click is not None
            or type(expected_context_id) is not int
            or not isinstance(expected_binding_name, str)
            or not isinstance(expected_frame_id, str)
            or not isinstance(event, dict)
        ):
            return
        event_dict = cast(dict[str, object], event)
        context_id = event_dict.get("executionContextId")
        payload = event_dict.get("payload")
        if (
            event_dict.get("name") != expected_binding_name
            or type(context_id) is not int
            or context_id != expected_context_id
            or self._ui_diagnostic_cdp_context_frames.get(context_id) != expected_frame_id
            or not isinstance(payload, str)
            or len(payload) > 4_096
        ):
            return
        try:
            parsed_payload = json.loads(payload)
        except TypeError, json.JSONDecodeError:
            return
        if not isinstance(parsed_payload, dict):
            return
        value = cast(dict[str, object], parsed_payload)
        expected_keys = {
            "event_is_trusted",
            "source_route",
            "role",
            "tag",
            "label",
            "within_unique_navigation",
            "stable_semantic_locator_unique",
            "before_searchboxes",
            "is_post_permalink",
            "target_path",
        }
        source_route = value.get("source_route")
        role = value.get("role")
        tag = value.get("tag")
        label = value.get("label")
        within_unique_navigation = value.get("within_unique_navigation")
        stable_semantic_locator_unique = value.get("stable_semantic_locator_unique")
        before_searchboxes = value.get("before_searchboxes")
        is_post_permalink = value.get("is_post_permalink")
        target_path = value.get("target_path")
        if (
            set(value) != expected_keys
            or value.get("event_is_trusted") is not True
            or source_route != expected_route
            or not isinstance(role, str)
            or role not in {"BUTTON", "LINK", "OTHER"}
            or not isinstance(tag, str)
            or tag
            not in {
                "A",
                "BUTTON",
                "INPUT",
                "TEXTAREA",
                "NAV",
                "MAIN",
                "ARTICLE",
                "HEADER",
                "SECTION",
                "ASIDE",
                "DIV",
                "FORM",
                "OTHER",
            }
            or not isinstance(label, str)
            or label not in {"HOME", "SEARCH", "PROFILE", "FOR_YOU", "TOP", "RECENT", "OTHER"}
            or type(within_unique_navigation) is not bool
            or type(stable_semantic_locator_unique) is not bool
            or not isinstance(before_searchboxes, str)
            or before_searchboxes not in {"ZERO", "ONE", "MANY"}
            or type(is_post_permalink) is not bool
            or (target_path is not None and not isinstance(target_path, str))
        ):
            return
        safe_path = (
            target_path
            if isinstance(target_path, str)
            and re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/post/[A-Za-z0-9_-]{1,120}", target_path)
            else None
        )
        if bool(safe_path) is not is_post_permalink:
            return
        self._ui_diagnostic_operator_click = {
            "source_route": source_route,
            "role": role,
            "tag": tag,
            "label": label,
            "within_unique_navigation": within_unique_navigation,
            "stable_semantic_locator_unique": stable_semantic_locator_unique,
            "before_searchboxes": before_searchboxes,
            "is_post_permalink": is_post_permalink,
        }
        self._ui_diagnostic_operator_click_path = safe_path
        self._ui_diagnostic_action_status = "VERIFIED"
        self._ui_diagnostic_click_armed_route = None
        self._ui_diagnostic_click_context_id = None
        self._ui_diagnostic_click_binding_name = None
        self._ui_diagnostic_click_main_frame_id = None
        self._ui_diagnostic_cdp_context_frames.clear()

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self._ensure_alive()
        normalized_allowed_origins = frozenset(
            origin
            for allowed_origin in allowed_origins
            if (origin := _normalized_browser_origin(allowed_origin)) is not None
        )
        if not normalized_allowed_origins:
            raise UnsupportedUIState()
        if self._allowed_navigation_origins and (
            normalized_allowed_origins != self._allowed_navigation_origins
        ):
            raise UnsupportedUIState()
        if _normalized_browser_origin(url) not in normalized_allowed_origins:
            raise UnsupportedUIState()

        self._allowed_navigation_origins = normalized_allowed_origins
        self._remote_state_uncertain = False

        try:
            if not self._navigation_guard_installed:
                await self._page.route("**/*", self._guard_navigation)
                self._navigation_guard_installed = True
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )
            await self._raise_navigation_policy_error()
        except PlaywrightTimeoutError:
            await self._raise_navigation_policy_error()
            raise NavigationTimeout() from None
        except PlaywrightError:
            self._ensure_alive()
            await self._raise_navigation_policy_error()
            raise BrowserRuntimeUnavailable("BROWSER_NAVIGATION_FAILED") from None

    async def _guard_navigation(self, route: Route) -> None:
        request = route.request
        try:
            is_main_navigation = (
                request.is_navigation_request() and request.frame == self._page.main_frame
            )
        except PlaywrightError:
            is_main_navigation = True
        if self._synthetic_fixture_pages is not None:
            html = self._synthetic_fixture_pages.get(request.url)
            if html is not None:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
            else:
                self._synthetic_blocked_request_count += 1
                if is_main_navigation:
                    if _normalized_browser_origin(request.url) != self._async_ui_reader_origin:
                        self._remote_state_uncertain = True
                    else:
                        self._synthetic_fixture_missing = True
                await route.abort("blockedbyclient")
            return
        if not is_main_navigation:
            await route.continue_()
            return
        if _normalized_browser_origin(request.url) not in self._allowed_navigation_origins:
            self._remote_state_uncertain = True
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _raise_navigation_policy_error(self) -> None:
        await self._raise_guard_rejection()
        if (
            self._allowed_navigation_origins
            and self._page.url != "about:blank"
            and _normalized_browser_origin(self._page.url) not in self._allowed_navigation_origins
        ):
            raise RemoteSessionStateUncertain()

    async def _raise_ui_policy_error(self) -> None:
        await self._raise_navigation_policy_error()
        if self._synthetic_fixture_missing:
            raise BrowserAdapterError("SYNTHETIC_FIXTURE_PAGE_MISSING")

    def _get_async_ui_reader(self, origin: str) -> AsyncThreadsJobUiReader:
        normalized_origin = _normalized_browser_origin(origin)
        if (
            normalized_origin is None
            or self._allowed_navigation_origins != frozenset({normalized_origin})
            or (
                self._async_ui_reader_origin is not None
                and normalized_origin != self._async_ui_reader_origin
            )
        ):
            raise UnsupportedUIState()
        if self._async_ui_reader is None:
            self._async_ui_reader = AsyncThreadsJobUiReader(self._page, origin=normalized_origin)
            self._async_ui_reader_origin = normalized_origin
        return self._async_ui_reader

    async def _raise_guard_rejection(self) -> None:
        if self._remote_state_uncertain:
            raise RemoteSessionStateUncertain()

    async def collect_feed_permalinks(self, *, candidate_bound: int) -> tuple[str, ...]:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if (
            type(candidate_bound) is not int
            or not 1 <= candidate_bound <= BROWSER_FEED_CANDIDATE_BOUND
        ):
            raise BrowserContractError()
        loop = asyncio.get_running_loop()
        readiness_timeout_seconds = min(
            _BROWSER_READ_READINESS_TIMEOUT_SECONDS,
            self._navigation_timeout_ms / 1000,
        )
        deadline = loop.time() + readiness_timeout_seconds
        while True:
            permalinks = await self._scan_feed_permalinks(candidate_bound=candidate_bound)
            if permalinks:
                self._last_feed_permalinks = frozenset(permalinks)
                return permalinks
            remaining = deadline - loop.time()
            if remaining <= 0:
                return ()
            await asyncio.sleep(min(_BROWSER_READ_READINESS_POLL_SECONDS, remaining))
            if loop.time() >= deadline:
                return ()

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        target_match = re.fullmatch(
            r"/@([A-Za-z0-9._]{1,30})/post/[A-Za-z0-9_-]{1,120}", target_ref
        )
        if target_match is None or target_match.group(1) != author_username:
            raise BrowserContractError()
        loop = asyncio.get_running_loop()
        readiness_timeout_seconds = min(
            _BROWSER_READ_READINESS_TIMEOUT_SECONDS,
            self._navigation_timeout_ms / 1000,
        )
        deadline = loop.time() + readiness_timeout_seconds
        script_args = {
            "allowedOrigin": BROWSER_FEED_ORIGIN,
            "targetRef": target_ref,
            "targetAuthor": author_username,
        }
        try:
            payload = await self._page.evaluate(
                _THREAD_OPEN_SCRIPT,
                script_args,
            )
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_THREAD_INSPECTION_FAILED") from None
        outcome = _thread_target_outcome(payload)
        while outcome in {"missing_permalink", "missing_author"}:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_BROWSER_READ_READINESS_POLL_SECONDS, remaining))
            if loop.time() >= deadline:
                break
            try:
                payload = await self._page.evaluate(_THREAD_OPEN_SCRIPT, script_args)
            except PlaywrightError:
                self._ensure_alive()
                raise BrowserRuntimeUnavailable("BROWSER_THREAD_INSPECTION_FAILED") from None
            outcome = _thread_target_outcome(payload)
            if outcome == "recognized":
                return
        _verify_thread_target_result(payload)

    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if (
            not 1 <= ancestor_bound <= 8
            or re.fullmatch(r"/@[A-Za-z0-9._]{1,30}", target_ref) is None
        ):
            raise BrowserContractError()
        loop = asyncio.get_running_loop()
        readiness_timeout_seconds = min(
            _BROWSER_READ_READINESS_TIMEOUT_SECONDS,
            self._navigation_timeout_ms / 1000,
        )
        deadline = loop.time() + readiness_timeout_seconds
        script_args = {
            "allowedOrigin": BROWSER_FEED_ORIGIN,
            "targetRef": target_ref,
            "ancestorBound": ancestor_bound,
        }
        try:
            payload = await self._page.evaluate(_PROFILE_OPEN_SCRIPT, script_args)
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_PROFILE_INSPECTION_FAILED") from None

        while payload != {"outcome": "recognized"}:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_BROWSER_READ_READINESS_POLL_SECONDS, remaining))
            if loop.time() >= deadline:
                break
            try:
                payload = await self._page.evaluate(_PROFILE_OPEN_SCRIPT, script_args)
            except PlaywrightError:
                self._ensure_alive()
                raise BrowserRuntimeUnavailable("BROWSER_PROFILE_INSPECTION_FAILED") from None
            if payload == {"outcome": "recognized"}:
                return
        _verify_profile_target_result(payload)

    async def scroll_feed(self) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        previous_permalinks = self._last_feed_permalinks
        try:
            await self._page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight, 1))")
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_FEED_SCROLL_FAILED") from None

        loop = asyncio.get_running_loop()
        readiness_timeout_seconds = min(
            _BROWSER_READ_READINESS_TIMEOUT_SECONDS,
            self._navigation_timeout_ms / 1000,
        )
        deadline = loop.time() + readiness_timeout_seconds
        while True:
            permalinks = await self._scan_feed_permalinks(
                candidate_bound=BROWSER_FEED_CANDIDATE_BOUND
            )
            if permalinks:
                self._last_feed_permalinks = frozenset(permalinks)
                if self._last_feed_permalinks.difference(previous_permalinks):
                    return
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(_BROWSER_READ_READINESS_POLL_SECONDS, remaining))
            if loop.time() >= deadline:
                return

    async def _scan_feed_permalinks(self, *, candidate_bound: int) -> tuple[str, ...]:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        try:
            payload = await self._page.evaluate(
                _FEED_SCAN_SCRIPT,
                {
                    "allowedOrigin": BROWSER_FEED_ORIGIN,
                    "candidateBound": candidate_bound,
                },
            )
        except PlaywrightError:
            self._ensure_alive()
            await self._raise_navigation_policy_error()
            raise BrowserRuntimeUnavailable("BROWSER_FEED_INSPECTION_FAILED") from None
        await self._raise_navigation_policy_error()
        return _feed_permalinks(payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ui_diagnostic_click_armed_route = None
        self._ui_diagnostic_click_context_id = None
        self._ui_diagnostic_click_binding_name = None
        self._ui_diagnostic_click_main_frame_id = None
        self._ui_diagnostic_cdp_context_frames.clear()
        self._ui_diagnostic_operator_click = None
        self._ui_diagnostic_operator_click_path = None
        if self._ui_diagnostic_cdp_session is not None:
            try:
                await self._ui_diagnostic_cdp_session.detach()
            except Exception:  # noqa: BLE001, S110
                pass
            self._ui_diagnostic_cdp_session = None
        try:
            await self._context.close()
        except Exception:  # noqa: BLE001, S110
            # Best-effort teardown must not mask the operation result or leak backend details.
            pass
        finally:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001, S110
                # Stopping Playwright is best effort even when context teardown failed.
                pass

    def _ensure_alive(self) -> None:
        if (
            self._closed
            or self._crashed
            or self._page.is_closed()
            or (self._browser is not None and not self._browser.is_connected())
        ):
            raise BrowserProcessCrashed()

    def _on_page_crash(self, _: Page) -> None:
        self._crashed = True

    def _on_context_close(self, _: BrowserContext) -> None:
        if not self._closed:
            self._crashed = True


def _feed_permalinks(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if data.get("reason") == "origin_mismatch":
        raise RemoteSessionStateUncertain()
    if data.get("reason") == "candidate_bound_exceeded":
        raise BrowserAdapterError("BROWSER_FEED_CANDIDATE_BOUND_EXCEEDED")
    permalink_value = data.get("permalinks")
    if data.get("ok") is not True or not isinstance(permalink_value, list):
        raise BrowserContractError()
    permalinks = cast(list[object], permalink_value)
    if len(permalinks) > BROWSER_FEED_CANDIDATE_BOUND or not all(
        isinstance(permalink, str) for permalink in permalinks
    ):
        raise BrowserContractError()
    return tuple(cast(str, permalink) for permalink in permalinks)


def _thread_target_outcome(payload: object) -> str:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if set(data) != {"outcome"}:
        raise BrowserContractError()
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in {
        "recognized",
        "uncertain",
        "missing_permalink",
        "missing_author",
        "invalid",
    }:
        raise BrowserContractError()
    return outcome


def _verify_thread_target_result(payload: object) -> None:
    outcome = _thread_target_outcome(payload)
    if outcome == "uncertain":
        raise RemoteSessionStateUncertain()
    if outcome == "missing_permalink":
        raise RemoteSessionStateUncertain()
    if outcome != "recognized":
        raise BrowserContractError()


def _verify_profile_target_result(payload: object) -> None:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if set(data) != {"outcome"}:
        raise BrowserContractError()
    outcome = data.get("outcome")
    if outcome == "uncertain":
        raise RemoteSessionStateUncertain()
    if outcome != "recognized":
        raise BrowserContractError()


def playwright_proxy_settings(request: BrowserLaunchRequest) -> ProxySettings | None:
    route = request.network_route
    credentials = request.proxy_credentials
    if route.protocol is BrowserNetworkProtocol.DIRECT:
        if route.host is not None or route.port is not None or credentials is not None:
            raise BrowserNetworkRouteUnsupported()
        return None
    if not route.host or route.port is None:
        raise BrowserNetworkRouteUnsupported()
    scheme = {
        BrowserNetworkProtocol.HTTP: "http",
        BrowserNetworkProtocol.HTTPS: "https",
        BrowserNetworkProtocol.SOCKS5: "socks5",
    }.get(route.protocol)
    if scheme is None:
        raise BrowserNetworkRouteUnsupported()
    if (
        route.protocol is BrowserNetworkProtocol.SOCKS5
        and credentials is not None
        and (credentials.username is not None or credentials.password is not None)
    ):
        raise BrowserNetworkRouteUnsupported()
    server_host = (
        f"[{route.host}]" if ":" in route.host and not route.host.startswith("[") else route.host
    )
    settings: ProxySettings = {"server": f"{scheme}://{server_host}:{route.port}"}
    if credentials is not None:
        if credentials.username is not None:
            settings["username"] = credentials.username
        if credentials.password is not None:
            settings["password"] = credentials.password
    return settings


def _normalized_browser_origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"
