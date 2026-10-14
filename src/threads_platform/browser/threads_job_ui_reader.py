"""Bounded semantic UI reader for a search/feed and one exact post detail.

The selectors in this module are a conservative implementation candidate. They
have only been exercised against the offline synthetic Chromium fixture; live
Threads compatibility has not been verified.
"""

from __future__ import annotations

import asyncio
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
_READINESS_TIMEOUT_MS = 8_000
_READINESS_POLL_MS = 100
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
({expectedOrigin, expectedPath, expectedQuery, expectedActor, requireSearch, searchPanelMode = false}) => {
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
  else if (Array.from(document.querySelectorAll('iframe[title*="challenge" i], [aria-label*="captcha" i], [role="dialog"][aria-label*="challenge" i]')).some(visible)) {
    state = 'CHALLENGE';
  } else if (Array.from(document.querySelectorAll('form[action*="login" i], input[type="password"]')).some(visible)) {
    state = 'LOGIN_WALL';
  } else if (Array.from(document.querySelectorAll('[role="alert"][aria-label*="rate" i]')).some(visible)) {
    state = 'RATE_LIMITED';
  } else if (requireSearch ? !['/', '/search'].includes(pathname) : pathname !== expectedPath) state = 'REDIRECTED';

  const mains = Array.from(document.querySelectorAll('main, [role="main"]')).filter(visible);
  const navigations = Array.from(document.querySelectorAll('nav, [role="navigation"]')).filter(visible);
  const contentCardSelector = 'article, [role="article"]';
  const shellHeaders = Array.from(document.querySelectorAll('header')).filter((header) =>
    visible(header) && !header.closest(contentCardSelector) && !header.closest('main, [role="main"]')
  );
  const banners = Array.from(document.querySelectorAll('[role="banner"]')).filter(visible);
  const profilePath = (anchor) => {
    const href = anchor.getAttribute('href');
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, window.location.href);
      return url.origin === expectedOrigin && !url.search && !url.hash &&
        /^\/@[A-Za-z0-9._:-]{1,96}$/.test(url.pathname) ? url.pathname : null;
    } catch (_) { return null; }
  };
  const linksIn = (roots) => roots.flatMap((root) =>
    Array.from(root.querySelectorAll('a[href]')).filter(visible).map(profilePath)
      .filter((path) => typeof path === 'string')
  );
  const bannerLinks = linksIn(banners);
  const controlLabel = (control) =>
    (control.getAttribute('aria-label') || control.getAttribute('title') || control.innerText || '')
      .replace(/\s+/g, ' ').trim().toLocaleLowerCase();
  const profileLabels = new Set(['profile', 'your profile', 'my profile', 'account', 'your account', 'my account']);
  const controlsIn = (root) => Array.from(root.querySelectorAll('a[href], button, [role="link"], [role="button"]'))
    .filter(visible);
  const navControls = navigations.length === 1 ? controlsIn(navigations[0]) : [];
  const selfProfileControls = navControls.filter((control) => profileLabels.has(controlLabel(control)));
  const actorLinks = selfProfileControls.map(profilePath).filter((path) => typeof path === 'string');
  const navigationLabels = navControls.map(controlLabel);
  const hasHomeDestination = navigationLabels.some((label) =>
    ['home', 'for you', 'home / for you', 'home for you'].includes(label)
  );
  const hasSearchDestination = navigationLabels.includes('search');
  // Keep explicit labelled header affordances for older supported layouts; an
  // arbitrary banner/profile link is never account identity evidence.
  const explicitHeaderActorLinks = shellHeaders.flatMap((root) =>
    Array.from(root.querySelectorAll('a[href]')).filter((anchor) => visible(anchor) &&
      profileLabels.has(controlLabel(anchor))).map(profilePath).filter((path) => typeof path === 'string')
  );
  const legacyActorLinks = explicitHeaderActorLinks;
  const verifiedActorLinks = navigations.length === 1 ? actorLinks : legacyActorLinks;
  const searchRoots = Array.from(document.querySelectorAll(
    'aside, [role="complementary"], [role="dialog"], [role="region"][aria-label]'
  )).filter(visible);
  const searchInputs = requireSearch
    ? Array.from(document.querySelectorAll(searchPanelMode
      ? 'input[type="search"], [role="searchbox"]'
      : 'form[role="search"] input[type="search"]')).filter(visible)
    : [];
  const searchPanelCandidates = searchRoots.filter((root) => {
    const rootInputs = searchInputs.filter((input) => root.contains(input));
    const label = controlLabel(root);
    return rootInputs.length === 1 && (root.matches('aside, [role="complementary"]') ||
      label === 'search' || label.startsWith('search '));
  });
  const searchRootCandidates = searchPanelCandidates.filter((root) =>
    !searchPanelCandidates.some((other) => other !== root && root.contains(other))
  );
  const searchValues = searchInputs.map((input) => input.value || '');
  const cardSelector = 'article, [role="article"]';
  const rootCards = Array.from(document.querySelectorAll(cardSelector)).filter((card) => {
    const parent = card.parentElement;
    return visible(card) && (!parent || !parent.closest(cardSelector));
  });
  const detailRootCount = expectedPath.startsWith('/@')
    ? Array.from(document.querySelectorAll('a[href]')).filter((anchor) => {
        if (!visible(anchor) || anchor.closest('nav, aside, footer, blockquote')) return false;
        try {
          const url = new URL(anchor.getAttribute('href'), window.location.href);
          return url.origin === expectedOrigin && !url.search && !url.hash &&
            url.pathname === expectedPath &&
            /^\/@[A-Za-z0-9._:-]{1,96}\/(?:post-[A-Za-z0-9._:-]{1,128}|post\/[A-Za-z0-9._:-]{1,128})$/.test(url.pathname);
        } catch (_) { return false; }
      }).length
    : 0;
  const visibleBodyChildCount = document.body
    ? Array.from(document.body.children).filter(visible).length
    : 0;
  return {
    state,
    origin,
    pathname,
    mains: mains.length,
    navigations: navigations.length,
    hasHomeDestination,
    hasSearchDestination,
    selfProfileControlCount: selfProfileControls.length,
    shellHeaders: shellHeaders.length,
    bannerLinks,
    searchRootCount: searchRootCandidates.length,
    actorLinks: verifiedActorLinks,
    searchValues,
    detailRootCount,
    searchPanelMode,
    visibleBodyChildCount
  };
}
"""

_FEED_BATCH_SCRIPT = r"""
({batchIndex, maxCards, expectedQuery, searchPanel = false}) => {
  const mainSelector = 'main, [role="main"]';
  const cardSelector = 'article, [role="article"]';
  const renderedInPage = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let current = element;
    while (current) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 ||
          current.getAttribute('aria-hidden') === 'true') return false;
      current = current.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const mains = Array.from(document.querySelectorAll(mainSelector)).filter(renderedInPage);
  const searchInputs = Array.from(document.querySelectorAll('input[type="search"], [role="searchbox"]'))
    .filter(renderedInPage);
  const panels = Array.from(document.querySelectorAll(
    'aside, [role="complementary"], [role="dialog"], [role="region"][aria-label]'
  )).filter(renderedInPage);
  const panelCandidates = panels.filter((panel) => {
    const inputs = searchInputs.filter((input) => panel.contains(input));
    const label = (panel.getAttribute('aria-label') || panel.getAttribute('title') || '')
      .replace(/\s+/g, ' ').trim().toLocaleLowerCase();
    return inputs.length === 1 && (panel.matches('aside, [role="complementary"]') ||
      label === 'search' || label.startsWith('search '));
  });
  const searchRoots = panelCandidates.filter((panel) =>
    !panelCandidates.some((other) => other !== panel && panel.contains(other))
  );
  const searchRoot = searchPanel
    ? (searchRoots.length === 1 && searchInputs.length === 1 &&
      searchInputs[0].value === expectedQuery ? searchRoots[0] : null)
    : null;
  if (searchPanel && !searchRoot) return {ok: false, reason: 'root'};
  if (!searchPanel && mains.length !== 1) return {ok: false, reason: 'root'};
  const main = searchPanel ? searchRoot : mains[0];
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
    if (!searchPanel) return rect.bottom > 0 && rect.top < window.innerHeight;
    let clip = {top: 0, right: window.innerWidth, bottom: window.innerHeight, left: 0};
    for (let current = element.parentElement; current; current = current.parentElement) {
      const style = window.getComputedStyle(current);
      if (/auto|scroll|hidden|clip/.test(`${style.overflowX} ${style.overflowY}`)) {
        const parentRect = current.getBoundingClientRect();
        clip = {
          top: Math.max(clip.top, parentRect.top),
          right: Math.min(clip.right, parentRect.right),
          bottom: Math.min(clip.bottom, parentRect.bottom),
          left: Math.max(clip.left, parentRect.left)
        };
      }
    }
    return rect.bottom > clip.top && rect.top < clip.bottom &&
      rect.right > clip.left && rect.left < clip.right;
  };
  const canonicalPath = (href) => {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, window.location.href);
      return url.origin === window.location.origin && !url.search && !url.hash
        ? url.pathname : null;
    } catch (_) { return null; }
  };
  const isPostPath = (path) => typeof path === 'string' &&
    /^\/@[A-Za-z0-9._:-]{1,96}\/(?:post-[A-Za-z0-9._:-]{1,128}|post\/[A-Za-z0-9._:-]{1,128})$/.test(path);
  const profilePath = (href) => {
    const path = canonicalPath(href);
    return typeof path === 'string' && /^\/@[A-Za-z0-9._:-]{1,96}$/.test(path) ? path : null;
  };
  const allCards = Array.from(main.querySelectorAll(cardSelector));
  const allPostAnchors = Array.from(main.querySelectorAll('a[href]')).filter((anchor) =>
    renderedInPage(anchor) && !anchor.closest('blockquote') && !anchor.closest('nav, footer') &&
    isPostPath(canonicalPath(anchor.getAttribute('href')))
  );
  if (allCards.length + allPostAnchors.length > 1000) return {ok: false, reason: 'dom_bound'};
  const roots = allCards.filter((card) => {
    const parent = card.parentElement;
    return (!parent || !parent.closest(cardSelector)) && visible(card) &&
      (searchPanel || !card.closest('aside, nav, footer'));
  });
  if (searchPanel) {
    const visualRoots = new Set();
    for (const anchor of allPostAnchors) {
      if (anchor.closest(cardSelector)) continue;
      let current = anchor.parentElement;
      for (let depth = 0; current && current !== main && depth < 12; depth++, current = current.parentElement) {
        if (current.matches('nav, footer, blockquote') || !visible(current)) continue;
        const anchors = Array.from(current.querySelectorAll('a[href]')).filter((candidate) =>
          rendered(candidate) && !candidate.closest('blockquote, nav, footer')
        );
        const posts = anchors.map((candidate) => canonicalPath(candidate.getAttribute('href')))
          .filter(isPostPath);
        const authors = Array.from(new Set(anchors.map((candidate) =>
          profilePath(candidate.getAttribute('href'))).filter((path) => typeof path === 'string')));
        if (posts.length === 1 && authors.length === 1) {
          visualRoots.add(current);
          break;
        }
      }
    }
    roots.push(...Array.from(visualRoots));
  }
  if (roots.length > maxCards) return {ok: false, reason: 'card_bound'};
  const cards = [];
  for (const card of roots) {
    const semanticCard = card.matches(cardSelector);
    const owns = (element) => semanticCard ? element.closest(cardSelector) === card : card.contains(element);
    const anchors = Array.from(card.querySelectorAll('a[href]')).filter(
      (anchor) => owns(anchor) && !anchor.closest('blockquote') && rendered(anchor)
    );
    const postLinks = anchors.map((anchor) => anchor.getAttribute('href')).filter((href) => {
      if (searchPanel) return isPostPath(canonicalPath(href));
      if (typeof href !== 'string') return false;
      try {
        return /\/post(?:\/|-)/.test(href) ||
          /\/post(?:\/|-)/.test(new URL(href, window.location.href).pathname);
      } catch (_) { return /\/post(?:\/|-)/.test(href); }
    });
    let headerLinks;
    if (searchPanel) {
      headerLinks = anchors.map((anchor) => anchor.getAttribute('href'))
        .filter((href) => profilePath(href) !== null);
    } else {
      headerLinks = [];
      for (const header of Array.from(card.querySelectorAll('header'))) {
        if (!owns(header) || header.closest('blockquote')) continue;
        for (const anchor of Array.from(header.querySelectorAll('a[href]'))) {
          if (owns(anchor) && rendered(anchor)) headerLinks.push(anchor.getAttribute('href'));
        }
      }
    }
    cards.push({postLinks, headerLinks});
  }
  const noResults = searchPanel && Array.from(main.querySelectorAll('[role="status"]')).some((status) =>
    rendered(status) && /^no results(?: found)?(?: for .+)?[.!]?$/i.test((status.innerText || '').trim())
  );
  const scrollCandidates = searchPanel && allPostAnchors.length
    ? [main, ...Array.from(main.querySelectorAll('*'))].filter((element) => {
        const style = window.getComputedStyle(element);
        return ['auto', 'scroll'].includes(style.overflowY) &&
          element.scrollHeight > element.clientHeight + 2 &&
          allPostAnchors.every((anchor) => element.contains(anchor));
      })
    : [];
  const scrollRoots = scrollCandidates.filter((element) =>
    !scrollCandidates.some((other) => other !== element && element.contains(other))
  );
  const scrollElement = searchPanel && scrollRoots.length === 1 ? scrollRoots[0] : main;
  const scrollY = Math.round(searchPanel ? scrollElement.scrollTop : window.scrollY);
  const viewport = Math.max(1, Math.round(searchPanel ? scrollElement.clientHeight : window.innerHeight));
  const scrollHeight = searchPanel ? scrollElement.scrollHeight :
    Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  return {
    ok: true,
    cards,
    noResults,
    scrollY,
    viewport,
    hasMore: scrollY + viewport < scrollHeight - 2,
    mainCount: mains.length,
    searchRootCount: searchRoots.length,
    batchIndex
  };
}
"""

_SCROLL_SCRIPT = r"""
((options) => {
  const searchPanel = Boolean(options && options.searchPanel === true);
  const visible = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && window.getComputedStyle(element).visibility !== 'hidden';
  };
  if (searchPanel) {
    const inputs = Array.from(document.querySelectorAll('input[type="search"], [role="searchbox"]')).filter(visible);
    const panels = Array.from(document.querySelectorAll(
      'aside, [role="complementary"], [role="dialog"], [role="region"][aria-label]'
    )).filter((panel) => visible(panel) && inputs.filter((input) => panel.contains(input)).length === 1);
    const roots = panels.filter((panel) => !panels.some((other) => other !== panel && panel.contains(other)));
    if (roots.length !== 1) return {before: 0, after: 0, step: 0};
    const root = roots[0];
    const pathPattern = /^\/@[A-Za-z0-9._:-]{1,96}\/(?:post-[A-Za-z0-9._:-]{1,128}|post\/[A-Za-z0-9._:-]{1,128})$/;
    const postAnchors = Array.from(root.querySelectorAll('a[href]')).filter((anchor) => {
      try {
        const url = new URL(anchor.getAttribute('href'), window.location.href);
        return url.origin === window.location.origin && !url.search && !url.hash && pathPattern.test(url.pathname);
      } catch (_) { return false; }
    });
    const candidates = postAnchors.length
      ? [root, ...Array.from(root.querySelectorAll('*'))].filter((element) => {
          const style = window.getComputedStyle(element);
          return ['auto', 'scroll'].includes(style.overflowY) &&
            element.scrollHeight > element.clientHeight + 2 &&
            postAnchors.every((anchor) => element.contains(anchor));
        })
      : [];
    const scrollRoots = candidates.filter((element) =>
      !candidates.some((other) => other !== element && element.contains(other))
    );
    const scrollElement = scrollRoots.length === 1 ? scrollRoots[0] : root;
    const before = Math.round(scrollElement.scrollTop);
    const step = Math.max(1, Math.floor(scrollElement.clientHeight * 0.8));
    scrollElement.scrollTop = before + step;
    return {before, after: Math.round(scrollElement.scrollTop), step};
  }
  const before = Math.round(window.scrollY);
  const step = Math.max(1, Math.floor(window.innerHeight * 0.8));
  window.scrollBy({top: step, left: 0, behavior: 'instant'});
  return {before, after: Math.round(window.scrollY), step};
})
"""

_DETAIL_SCRIPT = r"""
({target, maxBodyCodepoints}) => {
  const mainSelector = 'main, [role="main"]';
  const cardSelector = 'article, [role="article"]';
  const visible = (element) => {
    if (!element || element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let current = element;
    while (current) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 ||
          current.getAttribute('aria-hidden') === 'true') return false;
      current = current.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const mains = Array.from(document.querySelectorAll(mainSelector)).filter(visible);
  if (mains.length > 1) return {ok: false, reason: 'root'};
  // Exact post permalink evidence below scopes this fallback; the body alone never
  // qualifies a detail page or author.
  const main = mains.length === 1 ? mains[0] : document.body;
  const canonicalPath = (href) => {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href, window.location.href);
      return url.origin === window.location.origin && !url.search && !url.hash ? url.pathname : null;
    } catch (_) { return null; }
  };
  const targetPath = new URL(target).pathname;
  const postPathPattern = /^\/@([A-Za-z0-9._:-]{1,96})\/(?:post-[A-Za-z0-9._:-]{1,128}|post\/[A-Za-z0-9._:-]{1,128})$/;
  const profilePathPattern = /^\/@[A-Za-z0-9._:-]{1,96}$/;
  const isAuxiliary = (element) => {
    let current = element.parentElement;
    while (current && current !== main) {
      if (current.matches('nav, aside, footer')) return true;
      current = current.parentElement;
    }
    return current !== main;
  };
  const allCards = Array.from(main.querySelectorAll(cardSelector));
  if (allCards.length > 1000) return {ok: false, reason: 'dom_bound'};
  const roots = allCards.filter((card) => {
    const parent = card.parentElement;
    return (!parent || !parent.closest(cardSelector)) && visible(card) && !isAuxiliary(card);
  });
  const semanticMatches = [];
  for (const card of roots) {
    const ownAnchors = Array.from(card.querySelectorAll('a[href]')).filter(
      (anchor) => anchor.closest(cardSelector) === card && !anchor.closest('blockquote, nav, footer, aside') &&
        visible(anchor)
    );
    const postPaths = ownAnchors.map((anchor) => canonicalPath(anchor.getAttribute('href')))
      .filter((path) => typeof path === 'string' && postPathPattern.test(path));
    if (postPaths.includes(targetPath)) semanticMatches.push({card, ownAnchors, postPaths, semantic: true});
  }
  let candidates = semanticMatches;
  let failedAuthor = false;
  if (candidates.length === 0) {
    const targetAnchors = Array.from(main.querySelectorAll('a[href]')).filter((anchor) =>
      visible(anchor) && !anchor.closest('blockquote, nav, footer, aside') &&
      canonicalPath(anchor.getAttribute('href')) === targetPath
    );
    if (targetAnchors.length === 0) return {ok: false, reason: 'root'};
    if (targetAnchors.length > 1) return {ok: false, reason: 'permalink'};
    const anchor = targetAnchors[0];
    let fallbackCandidate = null;
    let current = anchor.parentElement;
    for (let depth = 0; current && current !== main && depth < 12; depth++, current = current.parentElement) {
      if (current.matches('blockquote, nav, footer, aside') || !visible(current)) continue;
      const ownAnchors = Array.from(current.querySelectorAll('a[href]')).filter((item) =>
        visible(item) && !item.closest('blockquote, nav, footer, aside')
      );
      const postPaths = ownAnchors.map((item) => canonicalPath(item.getAttribute('href')))
        .filter((path) => typeof path === 'string' && postPathPattern.test(path));
      if (postPaths.length !== 1 || postPaths[0] !== targetPath) continue;
      const headerAnchors = Array.from(current.querySelectorAll('header a[href]')).filter((item) =>
        visible(item) && !item.closest('blockquote, nav, footer, aside')
      );
      const profileAnchors = headerAnchors.length ? headerAnchors : ownAnchors;
      const authorPaths = Array.from(new Set(profileAnchors.map((item) => canonicalPath(item.getAttribute('href')))
        .filter((path) => typeof path === 'string' && profilePathPattern.test(path))));
      if (authorPaths.length === 1) {
        if (!fallbackCandidate) fallbackCandidate = {card: current, ownAnchors, postPaths, semantic: false};
        if (current.querySelector('div[dir="auto"], img')) {
          candidates = [{card: current, ownAnchors, postPaths, semantic: false}];
          break;
        }
        continue;
      }
      failedAuthor = true;
      if (authorPaths.length > 1) break;
    }
    if (candidates.length === 0 && fallbackCandidate) candidates = [fallbackCandidate];
    if (candidates.length === 0) return {ok: false, reason: failedAuthor ? 'author' : 'root'};
  }
  if (candidates.length !== 1) return {ok: false, reason: 'root'};
  const candidate = candidates[0];
  const owns = (element) => candidate.semantic
    ? element.closest(cardSelector) === candidate.card
    : candidate.card.contains(element);
  if (candidate.postPaths.length !== 1) return {ok: false, reason: 'permalink'};

  const headerAnchors = Array.from(candidate.card.querySelectorAll('header a[href]')).filter((anchor) =>
    owns(anchor) && !anchor.closest('blockquote, nav, footer, aside') && visible(anchor)
  );
  const profileAnchors = headerAnchors.length ? headerAnchors : candidate.ownAnchors;
  const authorPaths = Array.from(new Set(profileAnchors.map((anchor) => canonicalPath(anchor.getAttribute('href')))
    .filter((path) => typeof path === 'string' && profilePathPattern.test(path))));
  if (authorPaths.length !== 1) return {ok: false, reason: 'author'};

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
  const kind = !candidate.semantic ? 'UNKNOWN' : (quoted ? 'QUOTE' :
    (nestedCard ? 'UNKNOWN' :
      (/\breposted by\b/i.test(headerText) ? 'REPOST' :
        (/\breplied to\b/i.test(headerText) ? 'REPLY' : 'ORIGINAL'))));
  const imageOnly = !bodyText && Boolean(candidate.card.querySelector('img'));
  return {
    ok: true,
    canonicalPath: targetPath,
    authorPath: authorPaths[0],
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
                    "searchPanelMode": False,
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
        action_timeout_ms: int = 8_000,
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
        self._search_panel_mode = self._origin == BROWSER_FEED_ORIGIN
        page.set_default_timeout(action_timeout_ms)
        page.set_default_navigation_timeout(action_timeout_ms)

    @property
    def scroll_count(self) -> int:
        return self._scroll_count

    async def verify_signed_in(self, expected_actor: str) -> UiPageState:
        """Verify visible account UI before beginning a search cycle."""

        if not _USERNAME_RE.fullmatch(expected_actor):
            raise BrowserAdapterError("BROWSER_ACTOR_INVALID")
        home_url = f"{self._origin}/"
        if self._page.url != home_url:
            await self._goto(home_url)
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
        if self._search_panel_mode:
            await self._open_search_panel(query, expected_actor=expected_actor)
        else:
            search_url = f"{self._origin}/search?{urlencode({'q': query})}"
            await self._goto(search_url)
        batches: list[UiFeedBatch] = []
        no_results_seen = False
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
            snapshot = await self._feed_batch_snapshot(
                batch_index,
                query,
                search_panel=self._search_panel_mode,
                wait_for_cards=batch_index == 0,
            )
            raw_cards = snapshot.get("cards")
            if not isinstance(raw_cards, list):
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED")
            card_values = cast(list[object], raw_cards)
            if len(card_values) > self._max_cards_per_batch:
                raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
            try:
                cards = tuple(self._decode_card(item) for item in card_values)
            except BrowserAdapterError:
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED") from None
            no_results_seen = no_results_seen or snapshot.get("noResults") is True
            has_more = snapshot.get("hasMore")
            if not isinstance(has_more, bool):
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED")
            batches.append(UiFeedBatch(batch_index, page_url, page_state, cards, has_more))
            if not has_more or batch_index + 1 == self._max_batches:
                break
            scroll = _object(
                await self._evaluate(_SCROLL_SCRIPT, {"searchPanel": self._search_panel_mode})
            )
            before, after = scroll.get("before"), scroll.get("after")
            if type(before) is not int or type(after) is not int or after <= before:
                raise BrowserAdapterError("BROWSER_SCROLL_FAILED")
            self._scroll_count += 1
            await self._wait_for_render()
        if not batches or (not any(batch.cards for batch in batches) and not no_results_seen):
            raise BrowserAdapterError("SEARCH_UI_NOT_READY")
        return tuple(batches)

    async def _open_search_panel(self, query: str, *, expected_actor: str) -> None:
        home_url = f"{self._origin}/"
        if self._page.url != home_url:
            await self._goto(home_url)
        state, _ = await self._page_state(
            expected_path="/",
            expected_query=None,
            expected_actor=expected_actor,
            require_search=False,
        )
        if state is not UiPageState.READY:
            raise BrowserAdapterError("HOME_UI_NOT_READY")
        try:
            navigation = self._page.locator('nav:visible, [role="navigation"]:visible')
            if await navigation.count() != 1:
                raise BrowserAdapterError("SEARCH_CONTROL_UNRECOGNIZED")
            links = navigation.get_by_role("link", name="Search", exact=True)
            buttons = navigation.get_by_role("button", name="Search", exact=True)
            link_count = await links.count()
            button_count = await buttons.count()
            if link_count + button_count != 1:
                raise BrowserAdapterError("SEARCH_CONTROL_UNRECOGNIZED")
            await (links if link_count == 1 else buttons).click()
            inputs = self._page.locator('input[type="search"]:visible, [role="searchbox"]:visible')
            await inputs.first.wait_for(state="visible", timeout=self._timeout_ms)
            if await inputs.count() != 1:
                raise BrowserAdapterError("SEARCH_ROOT_UNRECOGNIZED")
            await inputs.first.fill(query)
            await inputs.first.press("Enter")
        except BrowserAdapterError:
            raise
        except AsyncPlaywrightTimeoutError:
            raise BrowserAdapterError("SEARCH_UI_NOT_READY") from None
        except AsyncPlaywrightError:
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("SEARCH_CONTROL_UNRECOGNIZED") from None
        except Exception:  # noqa: BLE001 - browser backend details stay private.
            if self._page.is_closed():
                raise BrowserProcessCrashed() from None
            raise BrowserAdapterError("SEARCH_CONTROL_UNRECOGNIZED") from None

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
        deadline = self._readiness_deadline()
        while True:
            try:
                detail_payload = await self._evaluate(
                    _DETAIL_SCRIPT,
                    {"target": canonical_target, "maxBodyCodepoints": _MAX_BODY_CODEPOINTS},
                )
            except BrowserReadTimeout:
                raise BrowserAdapterError("DETAIL_ROOT_UNRECOGNIZED") from None
            try:
                snapshot = _object(detail_payload)
            except BrowserAdapterError:
                raise BrowserAdapterError("DETAIL_ROOT_UNRECOGNIZED") from None
            if snapshot.get("ok") is True:
                break
            reason = snapshot.get("reason")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                code = (
                    "DETAIL_ROOT_UNRECOGNIZED"
                    if reason == "root"
                    else "DETAIL_PROVENANCE_UNVERIFIED"
                )
                raise BrowserAdapterError(code)
            await self._wait_for_render(remaining_ms=max(1, int(remaining * 1000)))
        if snapshot.get("canonicalPath") != target_path:
            raise BrowserAdapterError("DETAIL_PROVENANCE_UNVERIFIED")
        author_path = snapshot.get("authorPath")
        if not isinstance(author_path, str):
            raise BrowserAdapterError("DETAIL_PROVENANCE_UNVERIFIED")
        profile = _PROFILE_PATH_RE.fullmatch(author_path)
        if (
            profile is None
            or profile.group("username") != _parse_post_target(canonical_target, self._origin)[2]
        ):
            raise BrowserAdapterError("DETAIL_PROVENANCE_UNVERIFIED")
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
        target_values = [
            _parse_post_target(item, self._origin)[0]
            for item in cast(list[object], raw_posts)
            if isinstance(item, str)
        ]
        if len(target_values) != 1:
            raise BrowserAdapterError(
                "BROWSER_PERMALINK_MISSING" if not target_values else "BROWSER_PERMALINK_MISMATCH"
            )
        author_values = {
            author
            for item in cast(list[object], raw_authors)
            if isinstance(item, str)
            and (author := _parse_profile_link(item, self._origin)) is not None
        }
        if len(author_values) != 1:
            raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
        target = target_values[0]
        _, _, permalink_author = _parse_post_target(target, self._origin)
        author = next(iter(author_values))
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
        stage = "HOME" if expected_path == "/" else "SEARCH" if require_search else "DETAIL"
        deadline = self._readiness_deadline()
        last_result: dict[str, object] = {}
        while True:
            page_url = self._page.url
            try:
                parsed = urlsplit(page_url)
            except ValueError:
                return UiPageState.REDIRECTED, page_url
            origin = urlsplit(self._origin)
            if parsed.scheme != "https" or parsed.netloc != origin.netloc:
                return UiPageState.REDIRECTED, page_url
            try:
                page_payload = await self._evaluate(
                    _PAGE_STATE_SCRIPT,
                    {
                        "expectedOrigin": self._origin,
                        "expectedPath": expected_path,
                        "expectedQuery": expected_query,
                        "expectedActor": expected_actor,
                        "requireSearch": require_search,
                        "searchPanelMode": self._search_panel_mode,
                    },
                )
            except BrowserReadTimeout:
                raise BrowserAdapterError(f"{stage}_UI_NOT_READY") from None
            try:
                result = _object(page_payload)
            except BrowserAdapterError:
                raise BrowserAdapterError(f"{stage}_ROOT_UNRECOGNIZED") from None
            last_result = result
            try:
                state = UiPageState(result.get("state"))
            except TypeError, ValueError:
                state = UiPageState.UNKNOWN
            if state is not UiPageState.READY:
                if state is not UiPageState.UNKNOWN:
                    return state, page_url
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise BrowserAdapterError(
                        self._readiness_error_code(stage, last_result, expected_query)
                    )
                await self._wait_for_render(remaining_ms=max(1, int(remaining * 1000)))
                continue

            if require_search:
                try:
                    parsed_query = parse_qs(parsed.query, strict_parsing=True)
                except ValueError:
                    return UiPageState.REDIRECTED, page_url
                allowed_paths = {"/search"}
                allowed_queries = [{"q": [expected_query]}]
                if self._search_panel_mode:
                    allowed_paths.add("/")
                    allowed_queries.append({})
                if (
                    expected_query is None
                    or parsed.path not in allowed_paths
                    or parsed_query not in allowed_queries
                    or parsed.fragment
                ):
                    raise BrowserAdapterError("BROWSER_QUERY_MISMATCH")
            elif parsed.path != expected_path or parsed.query or parsed.fragment:
                return UiPageState.REDIRECTED, page_url

            actor_links_value = result.get("actorLinks")
            actor_links = (
                cast(list[object], actor_links_value) if isinstance(actor_links_value, list) else []
            )
            actors = [
                actor
                for item in actor_links
                if (actor := _parse_profile_link(item, self._origin)) is not None
            ]
            if len(actors) == 1 and actors[0] != expected_actor:
                raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")

            mains = result.get("mains")
            navigations = result.get("navigations")
            search_values_value = result.get("searchValues")
            search_values = (
                cast(list[object], search_values_value)
                if isinstance(search_values_value, list)
                else []
            )
            root_ready: bool = mains == 1
            if stage == "HOME" and mains == 0:
                # The observed Home layout uses a navigation landmark without a
                # main landmark or accessible Home/Search destination labels.
                # Account identity remains separately guarded below by the one
                # explicitly labelled self-profile link inside that navigation.
                root_ready = navigations == 1
            elif stage == "SEARCH" and self._search_panel_mode:
                root_ready = result.get("searchRootCount") == 1
            elif stage == "SEARCH" and mains == 0:
                root_ready = len(search_values) == 1 and search_values[0] == expected_query
            elif stage == "DETAIL" and mains == 0:
                root_ready = result.get("detailRootCount") == 1

            query_ready = True
            if require_search:
                query_ready = len(search_values) == 1 and search_values[0] == expected_query

            self_profile_count = result.get("selfProfileControlCount")
            actor_ready = navigations == 1 and self_profile_count == 1 and len(actors) == 1
            if not self._search_panel_mode and navigations == 0:
                actor_ready = len(actors) == 1
            actor_ready = actor_ready and actors[0] == expected_actor
            if root_ready and query_ready and actor_ready:
                return UiPageState.READY, page_url

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserAdapterError(
                    self._readiness_error_code(stage, last_result, expected_query)
                )
            await self._wait_for_render(remaining_ms=max(1, int(remaining * 1000)))

    async def _feed_batch_snapshot(
        self,
        batch_index: int,
        query: str,
        *,
        search_panel: bool,
        wait_for_cards: bool,
    ) -> dict[str, object]:
        deadline = self._readiness_deadline()
        while True:
            try:
                payload = await self._evaluate(
                    _FEED_BATCH_SCRIPT,
                    {
                        "batchIndex": batch_index,
                        "maxCards": self._max_cards_per_batch,
                        "expectedQuery": query,
                        "searchPanel": search_panel,
                    },
                )
            except BrowserReadTimeout:
                raise BrowserAdapterError("SEARCH_UI_NOT_READY") from None
            try:
                snapshot = _object(payload)
            except BrowserAdapterError:
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED") from None
            if snapshot.get("ok") is not True:
                if snapshot.get("reason") == "root":
                    raise BrowserAdapterError("SEARCH_ROOT_UNRECOGNIZED")
                if snapshot.get("reason") == "card_bound":
                    raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED")
            cards = snapshot.get("cards")
            if not isinstance(cards, list):
                raise BrowserAdapterError("SEARCH_CARD_UNRECOGNIZED")
            if cards or snapshot.get("noResults") is True or not wait_for_cards:
                return snapshot
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserAdapterError("SEARCH_UI_NOT_READY")
            await self._wait_for_render(remaining_ms=max(1, int(remaining * 1000)))

    def _readiness_deadline(self) -> float:
        timeout_seconds = min(_READINESS_TIMEOUT_MS, self._timeout_ms) / 1000
        return asyncio.get_running_loop().time() + timeout_seconds

    @staticmethod
    def _readiness_error_code(
        stage: str,
        result: dict[str, object],
        expected_query: str | None,
    ) -> str:
        mains = result.get("mains")
        if stage == "HOME":
            root_ready = mains == 1 or (mains == 0 and result.get("navigations") == 1)
            if not root_ready:
                return (
                    "HOME_UI_NOT_READY"
                    if result.get("visibleBodyChildCount") == 0
                    else "HOME_ROOT_UNRECOGNIZED"
                )
            return "HOME_ACTOR_UNVERIFIED"
        if stage == "SEARCH":
            search_values = result.get("searchValues")
            values = cast(list[object], search_values) if isinstance(search_values, list) else []
            root_ready = (
                result.get("searchRootCount") == 1
                if result.get("searchPanelMode") is True
                else mains == 1 or (mains == 0 and len(values) == 1 and values[0] == expected_query)
            )
            if not root_ready:
                return (
                    "SEARCH_UI_NOT_READY"
                    if result.get("visibleBodyChildCount") == 0
                    else "SEARCH_ROOT_UNRECOGNIZED"
                )
            if len(values) == 1 and values[0] != expected_query:
                return "BROWSER_QUERY_MISMATCH"
            return "SEARCH_UI_NOT_READY" if not values else "SEARCH_ACTOR_UNVERIFIED"
        root_ready = mains == 1 or (mains == 0 and result.get("detailRootCount") == 1)
        if not root_ready:
            return "DETAIL_ROOT_UNRECOGNIZED"
        return "DETAIL_ACTOR_UNVERIFIED"

    async def _goto(self, url: str) -> None:
        try:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
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

    async def _wait_for_render(self, *, remaining_ms: int | None = None) -> None:
        try:
            timeout_ms = (
                _READINESS_POLL_MS
                if remaining_ms is None
                else min(_READINESS_POLL_MS, remaining_ms)
            )
            await self._page.wait_for_timeout(min(timeout_ms, self._timeout_ms))
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
