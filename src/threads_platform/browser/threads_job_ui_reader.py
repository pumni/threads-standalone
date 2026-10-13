"""Bounded semantic UI reader for a search/feed and one exact post detail.

The selectors in this module are a conservative implementation candidate. They
have only been exercised against the offline synthetic Chromium fixture; live
Threads compatibility has not been verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Error as AsyncPlaywrightError
from playwright.async_api import Page as AsyncPage
from playwright.async_api import TimeoutError as AsyncPlaywrightTimeoutError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserProcessCrashed,
    BrowserReadTimeout,
    NavigationTimeout,
)

_DEFAULT_ORIGIN = BROWSER_FEED_ORIGIN
_MAX_QUERY_CODEPOINTS = 120
_MAX_BATCHES = 5
_MAX_CARDS_PER_BATCH = 20
_MAX_TARGET_CODEPOINTS = 2_048
_MAX_BODY_CODEPOINTS = 10_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_POST_PATH_RE = re.compile(
    r"^/@(?P<username>[A-Za-z0-9._:-]{1,96})/"
    r"(?:post-[A-Za-z0-9._:-]{1,128}|post/[A-Za-z0-9._:-]{1,128})$"
)
_PROFILE_PATH_RE = re.compile(r"^/@(?P<username>[A-Za-z0-9._:-]{1,96})$")


class UiPageState(StrEnum):
    READY = "READY"
    REDIRECTED = "REDIRECTED"
    CHALLENGE = "CHALLENGE"
    LOGIN_WALL = "LOGIN_WALL"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


class UiBodyVisibility(StrEnum):
    FULL_VISIBLE = "FULL_VISIBLE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class UiPostKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    REPOST = "REPOST"
    QUOTE = "QUOTE"
    REPLY = "REPLY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True, repr=False)
class UiPostTarget:
    canonical_post_url: str = field(repr=False)
    author_username: str = field(repr=False)

    def __repr__(self) -> str:
        return "UiPostTarget(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class UiFeedBatch:
    batch_index: int
    page_url: str = field(repr=False)
    state: UiPageState
    cards: tuple[UiPostTarget, ...] = field(repr=False)
    has_more: bool

    def __repr__(self) -> str:
        return (
            f"UiFeedBatch(batch_index={self.batch_index}, state={self.state.value!r}, "
            f"card_count={len(self.cards)}, has_more={self.has_more})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class UiPostDetail:
    requested_url: str = field(repr=False)
    page_url: str = field(repr=False)
    canonical_post_url: str = field(repr=False)
    state: UiPageState
    author_username: str | None = field(default=None, repr=False)
    body: str | None = field(default=None, repr=False)
    body_visibility: UiBodyVisibility = UiBodyVisibility.UNKNOWN
    body_truncated: bool = False
    post_kind: UiPostKind = UiPostKind.UNKNOWN
    image_only: bool = False

    def __repr__(self) -> str:
        return (
            f"UiPostDetail(state={self.state.value!r}, "
            f"body_visibility={self.body_visibility.value!r}, "
            f"body_codepoints={len(self.body) if self.body is not None else 0})"
        )


_PAGE_STATE_SCRIPT = r"""
({expectedOrigin, expectedPath, expectedQuery, expectedActor, requireSearch}) => {
  const visible = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let current = element;
    while (current) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
        return false;
      }
      if (current.getAttribute('aria-hidden') === 'true') return false;
      current = current.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const origin = window.location.origin;
  const pathname = window.location.pathname;
  let state = 'READY';
  if (origin !== expectedOrigin) state = 'REDIRECTED';
  else if (document.querySelector('iframe[title*="challenge" i], [aria-label*="captcha" i], [role="dialog"][aria-label*="challenge" i]')) {
    state = 'CHALLENGE';
  } else if (document.querySelector('form[action*="login" i], input[type="password"]')) {
    state = 'LOGIN_WALL';
  } else if (document.querySelector('[role="alert"][aria-label*="rate" i]')) {
    state = 'RATE_LIMITED';
  } else if (pathname !== expectedPath) state = 'REDIRECTED';

  const mains = Array.from(document.querySelectorAll('main, [role="main"]'));
  const bannerLinks = Array.from(document.querySelectorAll('header[role="banner"] a[href]'))
    .filter(visible)
    .map((anchor) => anchor.getAttribute('href'));
  const searchValues = requireSearch
    ? Array.from(document.querySelectorAll('form[role="search"] input[type="search"]'))
        .filter(visible)
        .map((input) => input.value)
    : [];
  return {state, origin, pathname, mains: mains.length, bannerLinks, searchValues};
}
"""

_FEED_BATCH_SCRIPT = r"""
({batchIndex, maxCards}) => {
  const mainSelector = 'main, [role="main"]';
  const cardSelector = 'article, [role="article"]';
  const mains = Array.from(document.querySelectorAll(mainSelector));
  if (mains.length !== 1) return {ok: false, reason: 'main'};
  const main = mains[0];
  const rendered = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let current = element;
    while (current && current !== main) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 ||
          current.getAttribute('aria-hidden') === 'true') return false;
      current = current.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const visible = (element) => {
    if (!rendered(element)) return false;
    const rect = element.getBoundingClientRect();
    return rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const allCards = Array.from(main.querySelectorAll(cardSelector));
  if (allCards.length > 1000) return {ok: false, reason: 'dom_bound'};
  const isAuxiliary = (card) => {
    let current = card.parentElement;
    while (current && current !== main) {
      if (current.matches('aside, nav, footer')) return true;
      current = current.parentElement;
    }
    return current !== main;
  };
  const roots = allCards.filter((card) => {
    const parent = card.parentElement;
    return (!parent || !parent.closest(cardSelector)) && !isAuxiliary(card) && visible(card);
  });
  if (roots.length > maxCards) return {ok: false, reason: 'card_bound'};
  const cards = [];
  for (const card of roots) {
    const owns = (element) => element.closest(cardSelector) === card;
    const anchors = Array.from(card.querySelectorAll('a[href]')).filter(
      (anchor) => owns(anchor) && !anchor.closest('blockquote') && rendered(anchor)
    );
    const postLinks = anchors.map((anchor) => anchor.getAttribute('href')).filter((href) => {
      if (typeof href !== 'string') return false;
      try {
        return /\/post(?:\/|-)/.test(href) ||
          /\/post(?:\/|-)/.test(new URL(href, window.location.href).pathname);
      } catch (_) { return /\/post(?:\/|-)/.test(href); }
    });
    const headerLinks = [];
    for (const header of Array.from(card.querySelectorAll('header'))) {
      if (!owns(header) || header.closest('blockquote')) continue;
      for (const anchor of Array.from(header.querySelectorAll('a[href]'))) {
        if (owns(anchor) && rendered(anchor)) headerLinks.push(anchor.getAttribute('href'));
      }
    }
    cards.push({postLinks, headerLinks});
  }
  const scrollY = Math.round(window.scrollY);
  const viewport = Math.max(1, Math.round(window.innerHeight));
  const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  return {
    ok: true,
    cards,
    scrollY,
    viewport,
    hasMore: scrollY + viewport < scrollHeight - 2,
    mainCount: mains.length,
    batchIndex
  };
}
"""

_SCROLL_SCRIPT = r"""
() => {
  const before = Math.round(window.scrollY);
  const step = Math.max(1, Math.floor(window.innerHeight * 0.8));
  window.scrollBy({top: step, left: 0, behavior: 'instant'});
  return {before, after: Math.round(window.scrollY), step};
}
"""

_DETAIL_SCRIPT = r"""
({target, maxBodyCodepoints}) => {
  const mainSelector = 'main, [role="main"]';
  const cardSelector = 'article, [role="article"]';
  const mains = Array.from(document.querySelectorAll(mainSelector));
  if (mains.length !== 1) return {ok: false, reason: 'main'};
  const main = mains[0];
  const visible = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let current = element;
    while (current && current !== main) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 ||
          current.getAttribute('aria-hidden') === 'true') return false;
      current = current.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const allCards = Array.from(main.querySelectorAll(cardSelector));
  if (allCards.length > 1000) return {ok: false, reason: 'dom_bound'};
  const isAuxiliary = (card) => {
    let current = card.parentElement;
    while (current && current !== main) {
      if (current.matches('aside, nav, footer')) return true;
      current = current.parentElement;
    }
    return current !== main;
  };
  const roots = allCards.filter((card) => {
    const parent = card.parentElement;
    return (!parent || !parent.closest(cardSelector)) && !isAuxiliary(card);
  });
  const owns = (element, card) => element.closest(cardSelector) === card;
  const canonicalPath = (href) => {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, window.location.href);
      return url.origin === window.location.origin && !url.search && !url.hash ? url.pathname : null;
    } catch (_) { return null; }
  };
  const matches = [];
  for (const card of roots) {
    const ownAnchors = Array.from(card.querySelectorAll('a[href]')).filter(
      (anchor) => owns(anchor, card) && !anchor.closest('blockquote')
    );
    const postPaths = new Set(ownAnchors.map((anchor) => canonicalPath(anchor.getAttribute('href')))
      .filter((path) => typeof path === 'string' && /\/post(?:\/|-)/.test(path)));
    if (postPaths.has(new URL(target).pathname)) matches.push({card, ownAnchors, postPaths});
  }
  if (matches.length !== 1) return {ok: false, reason: 'root'};
  const candidate = matches[0];
  if (candidate.postPaths.size !== 1) return {ok: false, reason: 'permalink'};

  const authorPaths = new Set();
  for (const header of Array.from(candidate.card.querySelectorAll('header'))) {
    if (!owns(header, candidate.card) || header.closest('blockquote')) continue;
    for (const anchor of Array.from(header.querySelectorAll('a[href]'))) {
      if (!owns(anchor, candidate.card) || !visible(anchor)) continue;
      const path = canonicalPath(anchor.getAttribute('href'));
      if (typeof path === 'string' && /^\/@[A-Za-z0-9._:-]{1,96}$/.test(path)) {
        authorPaths.add(path);
      }
    }
  }
  if (authorPaths.size !== 1) return {ok: false, reason: 'author'};

  const bodies = Array.from(candidate.card.querySelectorAll('div[dir="auto"]')).filter((body) => {
    if (!owns(body, candidate.card) || !visible(body) || body.closest('header, blockquote, aside, nav, footer')) {
      return false;
    }
    if (body.querySelector('article, [role="article"], blockquote, aside, nav, footer')) return false;
    return !Array.from(body.querySelectorAll('div[dir="auto"]')).some((child) => owns(child, candidate.card));
  });
  if (bodies.length > 1) return {ok: false, reason: 'body'};
  const body = bodies[0] || null;
  const rawText = body ? (body.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const bodyText = rawText.slice(0, maxBodyCodepoints + 1);
  let partial = false;
  if (body) {
    const style = window.getComputedStyle(body);
    const clamp = Number.parseInt(style.webkitLineClamp || '0', 10);
    partial = body.getAttribute('aria-expanded') === 'false' ||
      (Number.isFinite(clamp) && clamp > 0) ||
      (style.overflowY === 'hidden' && body.scrollHeight > body.clientHeight + 1) ||
      Array.from(body.querySelectorAll('button, [role="button"]')).some((button) =>
        /\b(?:more|see more)\b/i.test(button.innerText || button.getAttribute('aria-label') || '')
      ) || rawText.length > maxBodyCodepoints;
  }
  const quoted = candidate.card.querySelector('blockquote');
  const headerText = Array.from(candidate.card.querySelectorAll('header'))
    .filter((header) => owns(header, candidate.card) && !header.closest('blockquote'))
    .map((header) => header.innerText || '').join(' ');
  const nestedCard = Boolean(candidate.card.querySelector('article, [role="article"]'));
  const kind = quoted ? 'QUOTE' :
    (nestedCard ? 'UNKNOWN' :
      (/\breposted by\b/i.test(headerText) ? 'REPOST' :
        (/\breplied to\b/i.test(headerText) ? 'REPLY' : 'ORIGINAL')));
  const imageOnly = !bodyText && Boolean(candidate.card.querySelector('img'));
  return {
    ok: true,
    canonicalPath: new URL(target).pathname,
    authorPath: Array.from(authorPaths)[0],
    body: bodyText || null,
    bodyCount: bodies.length,
    partial,
    kind,
    imageOnly,
    nestedCard
  };
}
"""


class ThreadsJobUiReader:
    """Read semantic visible post cards and exact details from one Playwright page."""

    def __init__(
        self,
        page: Page,
        *,
        origin: str = _DEFAULT_ORIGIN,
        action_timeout_ms: int = 2_000,
        max_batches: int = _MAX_BATCHES,
        max_cards_per_batch: int = _MAX_CARDS_PER_BATCH,
    ) -> None:
        if not _valid_origin(origin):
            raise BrowserAdapterError("BROWSER_ORIGIN_INVALID")
        if (
            type(action_timeout_ms) is not int
            or action_timeout_ms < 1
            or type(max_batches) is not int
            or not 1 <= max_batches <= _MAX_BATCHES
            or type(max_cards_per_batch) is not int
            or not 1 <= max_cards_per_batch <= _MAX_CARDS_PER_BATCH
        ):
            raise BrowserAdapterError("BROWSER_LIMIT_INVALID")
        self._page = page
        self._origin = origin.rstrip("/")
        self._timeout_ms = action_timeout_ms
        self._max_batches = max_batches
        self._max_cards_per_batch = max_cards_per_batch
        self._scroll_count = 0
        self._blocked_request_count = 0
        self._blocked_off_origin_navigation = False
        page.set_default_timeout(action_timeout_ms)
        page.set_default_navigation_timeout(action_timeout_ms)

    @property
    def scroll_count(self) -> int:
        return self._scroll_count

    @property
    def blocked_request_count(self) -> int:
        return self._blocked_request_count

    def add_blocked_request(self) -> None:
        self._blocked_request_count += 1

    def note_blocked_off_origin_navigation(self) -> None:
        self._blocked_off_origin_navigation = True

    def _initialize_metrics(self) -> None:
        self._scroll_count = 0
        self._blocked_request_count = 0

    def search(self, query: str, *, expected_actor: str) -> tuple[UiFeedBatch, ...]:
        """Search and copy each viewport's visible card identities before scrolling."""

        self._initialize_metrics()
        if not query.strip() or len(query) > _MAX_QUERY_CODEPOINTS:
            raise BrowserAdapterError("BROWSER_QUERY_INVALID")
        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        search_url = f"{self._origin}/search?{urlencode({'q': query})}"
        self._goto(search_url)
        batches: list[UiFeedBatch] = []
        for batch_index in range(self._max_batches):
            page_state, page_url = self._page_state(
                expected_path="/search",
                expected_query=query,
                expected_actor=expected_actor,
                require_search=True,
            )
            if page_state is not UiPageState.READY:
                batches.append(UiFeedBatch(batch_index, page_url, page_state, (), False))
                return tuple(batches)
            snapshot = self._evaluate(
                _FEED_BATCH_SCRIPT,
                {
                    "batchIndex": batch_index,
                    "maxCards": self._max_cards_per_batch,
                },
            )
            payload = _object(snapshot)
            if payload.get("ok") is not True:
                reason = payload.get("reason")
                code = (
                    "BROWSER_CARD_LIMIT_EXCEEDED"
                    if reason == "card_bound"
                    else "BROWSER_SELECTOR_DRIFT"
                )
                raise BrowserAdapterError(code)
            raw_cards = payload.get("cards")
            if not isinstance(raw_cards, list):
                raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
            card_values = cast(list[object], raw_cards)
            if len(card_values) > self._max_cards_per_batch:
                raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
            cards = tuple(self._decode_card(card) for card in card_values)
            has_more = payload.get("hasMore")
            if not isinstance(has_more, bool):
                raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
            batches.append(UiFeedBatch(batch_index, page_url, page_state, cards, has_more))
            if not has_more or batch_index + 1 == self._max_batches:
                break
            scroll = _object(self._evaluate(_SCROLL_SCRIPT, None))
            before, after = scroll.get("before"), scroll.get("after")
            if type(before) is not int or type(after) is not int or after <= before:
                raise BrowserAdapterError("BROWSER_SCROLL_FAILED")
            self._scroll_count += 1
            self._wait_for_render()
        if not batches or not any(batch.cards for batch in batches):
            raise BrowserAdapterError("BROWSER_EMPTY_FEED")
        return tuple(batches)

    def read_exact_post(
        self,
        target: str,
        *,
        expected_actor: str,
    ) -> UiPostDetail:
        """Navigate to one exact permalink and read only its top-level root article."""

        canonical_target, target_path, _target_author = _parse_post_target(target, self._origin)
        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        self._goto(canonical_target)
        state, page_url = self._page_state(
            expected_path=target_path,
            expected_query=None,
            expected_actor=expected_actor,
            require_search=False,
        )
        if state is not UiPageState.READY:
            return UiPostDetail(target, page_url, target, state)
        snapshot = _object(
            self._evaluate(
                _DETAIL_SCRIPT,
                {
                    "target": canonical_target,
                    "maxBodyCodepoints": _MAX_BODY_CODEPOINTS,
                },
            )
        )
        if snapshot.get("ok") is not True:
            reason = snapshot.get("reason")
            code = "BROWSER_ROOT_NOT_FOUND" if reason == "root" else "BROWSER_SELECTOR_DRIFT"
            raise BrowserAdapterError(code)
        canonical_path = snapshot.get("canonicalPath")
        author_path = snapshot.get("authorPath")
        if canonical_path != target_path:
            raise BrowserAdapterError("BROWSER_TARGET_MISMATCH")
        if not isinstance(author_path, str):
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        profile = _PROFILE_PATH_RE.fullmatch(author_path)
        if profile is None:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        body_value = snapshot.get("body")
        body = body_value if isinstance(body_value, str) else None
        partial = snapshot.get("partial") is True or len(body or "") > _MAX_BODY_CODEPOINTS
        if body is not None and len(body) > _MAX_BODY_CODEPOINTS:
            body = body[:_MAX_BODY_CODEPOINTS]
        raw_kind = snapshot.get("kind")
        try:
            kind = UiPostKind(raw_kind)
        except TypeError, ValueError:
            kind = UiPostKind.UNKNOWN
        visibility = (
            UiBodyVisibility.UNKNOWN
            if body is None
            else UiBodyVisibility.PARTIAL
            if partial
            else UiBodyVisibility.FULL_VISIBLE
        )
        return UiPostDetail(
            requested_url=canonical_target,
            page_url=page_url,
            canonical_post_url=canonical_target,
            state=state,
            author_username=profile.group("username"),
            body=body,
            body_visibility=visibility,
            body_truncated=partial,
            post_kind=kind,
            image_only=snapshot.get("imageOnly") is True,
        )

    def _decode_card(self, value: object) -> UiPostTarget:
        card = _object(value)
        raw_posts = card.get("postLinks")
        raw_authors = card.get("headerLinks")
        if not isinstance(raw_posts, list) or not isinstance(raw_authors, list):
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        post_values = cast(list[object], raw_posts)
        targets = {
            _parse_post_target(item, self._origin)[0]
            for item in post_values
            if isinstance(item, str)
        }
        if not targets:
            raise BrowserAdapterError("BROWSER_PERMALINK_MISSING")
        if len(targets) != 1:
            raise BrowserAdapterError("BROWSER_PERMALINK_MISMATCH")
        authors: set[str] = set()
        for raw_author in cast(list[object], raw_authors):
            if not isinstance(raw_author, str):
                continue
            author = _parse_profile_link(raw_author, self._origin)
            if author is not None:
                authors.add(author)
        if len(authors) != 1:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        target = next(iter(targets))
        _canonical, _path, permalink_author = _parse_post_target(target, self._origin)
        author = next(iter(authors))
        if author != permalink_author:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        return UiPostTarget(target, author)

    def _page_state(
        self,
        *,
        expected_path: str,
        expected_query: str | None,
        expected_actor: str,
        require_search: bool,
    ) -> tuple[UiPageState, str]:
        page_url = self._page.url
        if self._blocked_off_origin_navigation:
            return UiPageState.REDIRECTED, page_url
        try:
            parsed = urlsplit(page_url)
        except ValueError:
            return UiPageState.REDIRECTED, page_url
        if parsed.scheme != "https" or parsed.netloc != urlsplit(self._origin).netloc:
            return UiPageState.REDIRECTED, page_url
        result = _object(
            self._evaluate(
                _PAGE_STATE_SCRIPT,
                {
                    "expectedOrigin": self._origin,
                    "expectedPath": expected_path,
                    "expectedQuery": expected_query,
                    "expectedActor": expected_actor,
                    "requireSearch": require_search,
                },
            )
        )
        state_value = result.get("state")
        try:
            state = UiPageState(state_value)
        except TypeError, ValueError:
            return UiPageState.UNKNOWN, page_url
        if state is not UiPageState.READY:
            return state, page_url
        if result.get("mains") != 1:
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        banner_links = result.get("bannerLinks")
        if not isinstance(banner_links, list):
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        actors = {
            actor
            for item in cast(list[object], banner_links)
            if (actor := _parse_profile_link(item, self._origin)) is not None
        }
        if actors != {expected_actor}:
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        if require_search:
            query = expected_query
            parsed_query = parse_qs(parsed.query, strict_parsing=True)
            if (
                query is None
                or parsed.path != "/search"
                or parsed_query != {"q": [query]}
                or result.get("searchValues") != [query]
            ):
                raise BrowserAdapterError("BROWSER_QUERY_MISMATCH")
        elif parsed.path != expected_path or parsed.query or parsed.fragment:
            return UiPageState.REDIRECTED, page_url
        return UiPageState.READY, page_url

    def _goto(self, url: str) -> None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            self._page.wait_for_timeout(min(50, self._timeout_ms))
        except PlaywrightTimeoutError:
            raise NavigationTimeout() from None
        except PlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            if self._blocked_off_origin_navigation:
                return
            try:
                actual_origin = (
                    urlsplit(self._page.url).scheme + "://" + urlsplit(self._page.url).netloc
                )
            except ValueError:
                actual_origin = ""
            if actual_origin != self._origin:
                return
            raise BrowserAdapterError("BROWSER_NAVIGATION_FAILED") from None
        except Exception:  # noqa: BLE001 - browser failures are reduced to fixed safe codes.
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_NAVIGATION_FAILED") from None

    def _evaluate(self, script: str, argument: object) -> object:
        try:
            return self._page.evaluate(script, argument)
        except PlaywrightTimeoutError:
            raise BrowserReadTimeout() from None
        except PlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_READ_FAILED") from None
        except Exception:  # noqa: BLE001 - browser failures are reduced to fixed safe codes.
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_READ_FAILED") from None

    def _wait_for_render(self) -> None:
        try:
            self._page.wait_for_timeout(min(100, self._timeout_ms))
        except PlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserReadTimeout() from None


class AsyncThreadsJobUiReader:
    """Async adapter over the same bounded DOM semantics as the B1 reader."""

    def __init__(
        self,
        page: AsyncPage,
        *,
        origin: str = _DEFAULT_ORIGIN,
        action_timeout_ms: int = 5_000,
        max_batches: int = _MAX_BATCHES,
        max_cards_per_batch: int = _MAX_CARDS_PER_BATCH,
    ) -> None:
        if not _valid_origin(origin):
            raise BrowserAdapterError("BROWSER_ORIGIN_INVALID")
        if (
            type(action_timeout_ms) is not int
            or action_timeout_ms < 1
            or type(max_batches) is not int
            or not 1 <= max_batches <= _MAX_BATCHES
            or type(max_cards_per_batch) is not int
            or not 1 <= max_cards_per_batch <= _MAX_CARDS_PER_BATCH
        ):
            raise BrowserAdapterError("BROWSER_LIMIT_INVALID")
        self._page = page
        self._origin = origin.rstrip("/")
        self._timeout_ms = action_timeout_ms
        self._max_batches = max_batches
        self._max_cards_per_batch = max_cards_per_batch
        self._scroll_count = 0
        page.set_default_timeout(action_timeout_ms)
        page.set_default_navigation_timeout(action_timeout_ms)

    @property
    def scroll_count(self) -> int:
        return self._scroll_count

    async def verify_signed_in(self, expected_actor: str) -> UiPageState:
        """Verify visible account UI before beginning a search cycle."""

        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        await self._goto(f"{self._origin}/")
        state, _ = await self._page_state(
            expected_path="/",
            expected_query=None,
            expected_actor=expected_actor,
            require_search=False,
        )
        return state

    async def search(self, query: str, *, expected_actor: str) -> tuple[UiFeedBatch, ...]:
        """Search bounded visible feed batches, validating account UI at each step."""

        self._scroll_count = 0
        if not query.strip() or len(query) > _MAX_QUERY_CODEPOINTS:
            raise BrowserAdapterError("BROWSER_QUERY_INVALID")
        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        search_url = f"{self._origin}/search?{urlencode({'q': query})}"
        await self._goto(search_url)
        batches: list[UiFeedBatch] = []
        for batch_index in range(self._max_batches):
            page_state, page_url = await self._page_state(
                expected_path="/search",
                expected_query=query,
                expected_actor=expected_actor,
                require_search=True,
            )
            if page_state is not UiPageState.READY:
                batches.append(UiFeedBatch(batch_index, page_url, page_state, (), False))
                return tuple(batches)
            snapshot = _object(
                await self._evaluate(
                    _FEED_BATCH_SCRIPT,
                    {"batchIndex": batch_index, "maxCards": self._max_cards_per_batch},
                )
            )
            if snapshot.get("ok") is not True:
                code = (
                    "BROWSER_CARD_LIMIT_EXCEEDED"
                    if snapshot.get("reason") == "card_bound"
                    else "BROWSER_SELECTOR_DRIFT"
                )
                raise BrowserAdapterError(code)
            raw_cards = snapshot.get("cards")
            if not isinstance(raw_cards, list):
                raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
            card_values = cast(list[object], raw_cards)
            if len(card_values) > self._max_cards_per_batch:
                raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
            cards = tuple(self._decode_card(item) for item in card_values)
            has_more = snapshot.get("hasMore")
            if not isinstance(has_more, bool):
                raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
            batches.append(UiFeedBatch(batch_index, page_url, page_state, cards, has_more))
            if not has_more or batch_index + 1 == self._max_batches:
                break
            scroll = _object(await self._evaluate(_SCROLL_SCRIPT, None))
            before, after = scroll.get("before"), scroll.get("after")
            if type(before) is not int or type(after) is not int or after <= before:
                raise BrowserAdapterError("BROWSER_SCROLL_FAILED")
            self._scroll_count += 1
            await self._wait_for_render()
        if not batches or not any(batch.cards for batch in batches):
            raise BrowserAdapterError("BROWSER_EMPTY_FEED")
        return tuple(batches)

    async def read_exact_post(
        self,
        target: str,
        *,
        expected_actor: str,
    ) -> UiPostDetail:
        """Read one exact root post with B1's same-card author/body rules."""

        canonical_target, target_path, _ = _parse_post_target(target, self._origin)
        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        await self._goto(canonical_target)
        state, page_url = await self._page_state(
            expected_path=target_path,
            expected_query=None,
            expected_actor=expected_actor,
            require_search=False,
        )
        if state is not UiPageState.READY:
            return UiPostDetail(target, page_url, canonical_target, state)
        snapshot = _object(
            await self._evaluate(
                _DETAIL_SCRIPT,
                {"target": canonical_target, "maxBodyCodepoints": _MAX_BODY_CODEPOINTS},
            )
        )
        if snapshot.get("ok") is not True:
            code = (
                "BROWSER_ROOT_NOT_FOUND"
                if snapshot.get("reason") == "root"
                else "BROWSER_SELECTOR_DRIFT"
            )
            raise BrowserAdapterError(code)
        if snapshot.get("canonicalPath") != target_path:
            raise BrowserAdapterError("BROWSER_TARGET_MISMATCH")
        author_path = snapshot.get("authorPath")
        if not isinstance(author_path, str):
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        profile = _PROFILE_PATH_RE.fullmatch(author_path)
        if profile is None:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        body_value = snapshot.get("body")
        body = body_value if isinstance(body_value, str) else None
        partial = snapshot.get("partial") is True or len(body or "") > _MAX_BODY_CODEPOINTS
        if body is not None:
            body = body[:_MAX_BODY_CODEPOINTS]
        try:
            kind = UiPostKind(snapshot.get("kind"))
        except TypeError, ValueError:
            kind = UiPostKind.UNKNOWN
        visibility = (
            UiBodyVisibility.UNKNOWN
            if body is None
            else UiBodyVisibility.PARTIAL
            if partial
            else UiBodyVisibility.FULL_VISIBLE
        )
        return UiPostDetail(
            requested_url=canonical_target,
            page_url=page_url,
            canonical_post_url=canonical_target,
            state=state,
            author_username=profile.group("username"),
            body=body,
            body_visibility=visibility,
            body_truncated=partial,
            post_kind=kind,
            image_only=snapshot.get("imageOnly") is True,
        )

    def _decode_card(self, value: object) -> UiPostTarget:
        card = _object(value)
        raw_posts = card.get("postLinks")
        raw_authors = card.get("headerLinks")
        if not isinstance(raw_posts, list) or not isinstance(raw_authors, list):
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        targets = {
            _parse_post_target(item, self._origin)[0]
            for item in cast(list[object], raw_posts)
            if isinstance(item, str)
        }
        if len(targets) != 1:
            raise BrowserAdapterError(
                "BROWSER_PERMALINK_MISSING" if not targets else "BROWSER_PERMALINK_MISMATCH"
            )
        authors = {
            author
            for item in cast(list[object], raw_authors)
            if isinstance(item, str)
            and (author := _parse_profile_link(item, self._origin)) is not None
        }
        if len(authors) != 1:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        target = next(iter(targets))
        _, _, permalink_author = _parse_post_target(target, self._origin)
        author = next(iter(authors))
        if author != permalink_author:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        return UiPostTarget(target, author)

    async def _page_state(
        self,
        *,
        expected_path: str,
        expected_query: str | None,
        expected_actor: str,
        require_search: bool,
    ) -> tuple[UiPageState, str]:
        page_url = self._page.url
        try:
            parsed = urlsplit(page_url)
        except ValueError:
            return UiPageState.REDIRECTED, page_url
        origin = urlsplit(self._origin)
        if parsed.scheme != "https" or parsed.netloc != origin.netloc:
            return UiPageState.REDIRECTED, page_url
        result = _object(
            await self._evaluate(
                _PAGE_STATE_SCRIPT,
                {
                    "expectedOrigin": self._origin,
                    "expectedPath": expected_path,
                    "expectedQuery": expected_query,
                    "expectedActor": expected_actor,
                    "requireSearch": require_search,
                },
            )
        )
        try:
            state = UiPageState(result.get("state"))
        except TypeError, ValueError:
            return UiPageState.UNKNOWN, page_url
        if state is not UiPageState.READY:
            return state, page_url
        if result.get("mains") != 1:
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        banner_links = result.get("bannerLinks")
        if not isinstance(banner_links, list):
            raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
        actors = {
            actor
            for item in cast(list[object], banner_links)
            if (actor := _parse_profile_link(item, self._origin)) is not None
        }
        if actors != {expected_actor}:
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        if require_search:
            try:
                parsed_query = parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                return UiPageState.REDIRECTED, page_url
            if (
                expected_query is None
                or parsed.path != "/search"
                or parsed_query != {"q": [expected_query]}
                or result.get("searchValues") != [expected_query]
            ):
                raise BrowserAdapterError("BROWSER_QUERY_MISMATCH")
        elif parsed.path != expected_path or parsed.query or parsed.fragment:
            return UiPageState.REDIRECTED, page_url
        return UiPageState.READY, page_url

    async def _goto(self, url: str) -> None:
        try:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
            await self._page.wait_for_timeout(min(50, self._timeout_ms))
        except AsyncPlaywrightTimeoutError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise NavigationTimeout() from None
        except AsyncPlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            actual = _page_origin(self._page.url)
            if actual != self._origin:
                return
            raise BrowserAdapterError("BROWSER_NAVIGATION_FAILED") from None
        except Exception:  # noqa: BLE001 - browser backend details stay private.
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_NAVIGATION_FAILED") from None

    async def _evaluate(self, script: str, argument: object) -> object:
        try:
            return await self._page.evaluate(script, argument)
        except AsyncPlaywrightTimeoutError:
            raise BrowserReadTimeout() from None
        except AsyncPlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_READ_FAILED") from None
        except Exception:  # noqa: BLE001 - browser backend details stay private.
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("BROWSER_READ_FAILED") from None

    async def _wait_for_render(self) -> None:
        try:
            await self._page.wait_for_timeout(min(100, self._timeout_ms))
        except AsyncPlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserReadTimeout() from None


def _page_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _parse_post_target(value: object, origin: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value or len(value) > _MAX_TARGET_CODEPOINTS:
        raise BrowserAdapterError("BROWSER_PERMALINK_INVALID")
    try:
        parsed = urlsplit(value)
        base = urlsplit(origin)
    except ValueError:
        raise BrowserAdapterError("BROWSER_PERMALINK_INVALID") from None
    if (
        parsed.scheme not in ("", "https")
        or (parsed.netloc and parsed.netloc != base.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserAdapterError("BROWSER_WRONG_ORIGIN")
    path = parsed.path
    match = _POST_PATH_RE.fullmatch(path)
    if match is None:
        raise BrowserAdapterError("BROWSER_PERMALINK_INVALID")
    canonical = f"{origin}{path}"
    return canonical, path, match.group("username")


def _parse_profile_link(value: object, origin: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        base = urlsplit(origin)
    except ValueError:
        return None
    if parsed.scheme and parsed.scheme != "https":
        return None
    if parsed.netloc and parsed.netloc != base.netloc:
        return None
    if (
        parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    match = _PROFILE_PATH_RE.fullmatch(parsed.path)
    return None if match is None else match.group("username")


def _valid_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BrowserAdapterError("BROWSER_SELECTOR_DRIFT")
    return cast(dict[str, object], value)
