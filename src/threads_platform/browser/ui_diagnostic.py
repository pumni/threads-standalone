"""Privacy-bounded semantic output contract for the supervised UI diagnostic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

_MAX_DIAGNOSTIC_CANDIDATES = 3_000
_MAX_DIAGNOSTIC_NODES = 180
_MAX_DIAGNOSTIC_RELATIONS = 480

_CARDINALITIES = frozenset({"ZERO", "ONE", "MANY"})
_ROUTES = frozenset({"HOME", "SEARCH", "POST", "OTHER"})
_ROLES = frozenset(
    {
        "BUTTON",
        "LINK",
        "NAVIGATION",
        "MAIN",
        "ARTICLE",
        "SEARCHBOX",
        "TEXTBOX",
        "TAB",
        "DIALOG",
        "REGION",
        "HEADING",
        "GENERIC",
        "OTHER",
    }
)
_TAGS = frozenset(
    {
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
)
_LABELS = frozenset({"HOME", "SEARCH", "PROFILE", "FOR_YOU", "TOP", "RECENT", "OTHER"})
_RELATIONS = frozenset(
    {
        "CONTROL_DESCENDANT_OF_NAV",
        "CONTROL_SAME_NODE_AS_SELF_LINK",
        "PANEL_CONTAINS_SEARCHBOX",
        "RESULT_POST_AND_AUTHOR_SHARE_CONTAINER",
        "DETAIL_POST_AND_AUTHOR_SHARE_CONTAINER",
    }
)
_BODY_STATES = frozenset({"PRESENT", "PARTIAL", "ABSENT", "UNKNOWN"})
_OBSERVED_KEYS = frozenset(
    {
        "status",
        "route_class",
        "counts",
        "nodes",
        "relations",
        "operator_action",
        "query_match",
        "self_profile_match",
        "body_status",
        "document_ready",
    }
)
_COUNT_KEYS = (
    "navigation_landmarks",
    "main_landmarks",
    "header_landmarks",
    "article_landmarks",
    "search_controls",
    "profile_controls",
    "home_controls",
    "searchboxes",
    "self_profile_links",
    "other_profile_links",
    "post_permalinks",
    "author_profile_links",
    "panels",
    "visible_candidates",
    "offscreen_candidates",
    "clipped_candidates",
)


class UiDiagnosticSchemaError(ValueError):
    """Raised when browser output does not match the fixed privacy schema."""


@dataclass(frozen=True, slots=True)
class UiDiagnosticNode:
    id: int
    role: str
    tag: str
    label: str

    def to_document(self) -> dict[str, object]:
        return {"id": self.id, "role": self.role, "tag": self.tag, "label": self.label}


@dataclass(frozen=True, slots=True)
class UiDiagnosticRelation:
    source: int
    target: int
    relation: str

    def to_document(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
        }


@dataclass(frozen=True, slots=True)
class UiDiagnosticOperatorAction:
    source_route: str
    role: str
    tag: str
    label: str
    within_unique_navigation: bool
    is_post_permalink: bool
    destination_route: str
    destination_searchboxes: str
    search_query_entered: bool
    search_query_applied: bool
    search_destination_relation: bool
    clicked_post_matches_destination: bool
    unique_co_container_author_matches_clicked_post: bool

    def to_document(self) -> dict[str, object]:
        return {
            "source_route": self.source_route,
            "role": self.role,
            "tag": self.tag,
            "label": self.label,
            "within_unique_navigation": self.within_unique_navigation,
            "is_post_permalink": self.is_post_permalink,
            "destination_route": self.destination_route,
            "destination_searchboxes": self.destination_searchboxes,
            "search_query_entered": self.search_query_entered,
            "search_query_applied": self.search_query_applied,
            "search_destination_relation": self.search_destination_relation,
            "clicked_post_matches_destination": self.clicked_post_matches_destination,
            "unique_co_container_author_matches_clicked_post": (
                self.unique_co_container_author_matches_clicked_post
            ),
        }


@dataclass(frozen=True, slots=True)
class UiDiagnosticSnapshot:
    status: Literal["OBSERVED", "NODE_BOUND_EXCEEDED"]
    route_class: str | None = None
    counts: tuple[tuple[str, str], ...] = ()
    nodes: tuple[UiDiagnosticNode, ...] = ()
    relations: tuple[UiDiagnosticRelation, ...] = ()
    query_match: bool = False
    self_profile_match: bool = False
    body_status: str = "UNKNOWN"
    document_ready: bool = False
    operator_action: UiDiagnosticOperatorAction | None = None

    def to_document(self) -> dict[str, object]:
        if self.status == "NODE_BOUND_EXCEEDED":
            return {"status": self.status}
        return {
            "status": self.status,
            "route_class": self.route_class,
            "counts": dict(self.counts),
            "nodes": [node.to_document() for node in self.nodes],
            "relations": [relation.to_document() for relation in self.relations],
            "operator_action": (
                None if self.operator_action is None else self.operator_action.to_document()
            ),
            "query_match": self.query_match,
            "self_profile_match": self.self_profile_match,
            "body_status": self.body_status,
            "document_ready": self.document_ready,
        }


DIAGNOSTIC_SCRIPT = r"""
({allowedOrigin, expectedUsername, configuredTopic, maxCandidates, maxNodes, maxRelations,
  operatorClick, operatorClickPath}) => {
  const bucket = (count) => count === 0 ? 'ZERO' : count === 1 ? 'ONE' : 'MANY';
  const normalize = (value) => {
    const text = typeof value === 'string' ? value : '';
    return text.length > 240 ? '' : text.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
  };
  const visibilityOf = (element) => {
    if (!(element instanceof Element) || !element.isConnected) return 'HIDDEN';
    let opacity = 1;
    for (let current = element, depth = 0; current && depth < 32;
         current = current.parentElement, depth += 1) {
      if (current.hidden || current.hasAttribute('hidden') || current.hasAttribute('inert') ||
          normalize(current.getAttribute('aria-hidden')) === 'true') return 'HIDDEN';
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || style.contentVisibility === 'hidden') return 'HIDDEN';
      const ownOpacity = Number.parseFloat(style.opacity);
      if (Number.isFinite(ownOpacity)) opacity *= ownOpacity;
      if (opacity <= 0.01) return 'HIDDEN';
      if (!current.parentElement && current.tagName !== 'HTML') return 'HIDDEN';
    }
    let current = element;
    let depth = 0;
    for (; current && current.parentElement && depth < 32; current = current.parentElement, depth += 1) {
      // A bound overflow fails closed instead of treating a distant ancestor as visible.
    }
    if (current && current.parentElement) return 'HIDDEN';
    const rects = Array.from(element.getClientRects());
    if (rects.length === 0) return 'HIDDEN';
    const viewport = {left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight};
    let intersectsViewport = false;
    for (const rect of rects) {
      if (rect.right > viewport.left && rect.left < viewport.right &&
          rect.bottom > viewport.top && rect.top < viewport.bottom) {
        intersectsViewport = true;
        break;
      }
    }
    if (!intersectsViewport) return 'OFFSCREEN';
    const clippingRects = [];
    let hardClip = false;
    for (let ancestor = element.parentElement, level = 0;
         ancestor && level < 32; ancestor = ancestor.parentElement, level += 1) {
      const style = window.getComputedStyle(ancestor);
      if ((style.clipPath && style.clipPath !== 'none') ||
          (style.maskImage && style.maskImage !== 'none') ||
          (style.clip && style.clip !== 'auto')) hardClip = true;
      const clipsX = !['visible', 'unset', 'initial'].includes(style.overflowX);
      const clipsY = !['visible', 'unset', 'initial'].includes(style.overflowY);
      if (clipsX || clipsY) clippingRects.push({
        rect: ancestor.getBoundingClientRect(), clipsX, clipsY
      });
    }
    const ownStyle = window.getComputedStyle(element);
    if ((ownStyle.clipPath && ownStyle.clipPath !== 'none') ||
        (ownStyle.maskImage && ownStyle.maskImage !== 'none') ||
        (ownStyle.clip && ownStyle.clip !== 'auto')) hardClip = true;
    for (const rect of rects) {
      if (rect.right <= viewport.left || rect.left >= viewport.right ||
          rect.bottom <= viewport.top || rect.top >= viewport.bottom) continue;
      if (hardClip) return 'CLIPPED';
      let clipped = {
        left: Math.max(rect.left, viewport.left),
        top: Math.max(rect.top, viewport.top),
        right: Math.min(rect.right, viewport.right),
        bottom: Math.min(rect.bottom, viewport.bottom)
      };
      for (const clipping of clippingRects) {
        if (clipping.clipsX) {
          clipped.left = Math.max(clipped.left, clipping.rect.left);
          clipped.right = Math.min(clipped.right, clipping.rect.right);
        }
        if (clipping.clipsY) {
          clipped.top = Math.max(clipped.top, clipping.rect.top);
          clipped.bottom = Math.min(clipped.bottom, clipping.rect.bottom);
        }
        if (clipped.right <= clipped.left || clipped.bottom <= clipped.top) break;
      }
      if (clipped.right > clipped.left && clipped.bottom > clipped.top) return 'VISIBLE';
    }
    return intersectsViewport ? 'CLIPPED' : 'OFFSCREEN';
  };
  const visible = (element) => visibilityOf(element) === 'VISIBLE';
  const safeUrl = (href) => {
    if (typeof href !== 'string' || !href || href.length > 512 || href.startsWith('//')) return null;
    try {
      const url = new URL(href, allowedOrigin);
      if (url.origin !== allowedOrigin || url.username || url.password) return null;
      return url;
    } catch (_) {
      return null;
    }
  };
  const normalizePath = (path) => path.endsWith('/') && path !== '/' ? path.slice(0, -1) : path;
  const profilePattern = /^\/@[A-Za-z0-9._]{1,30}\/?$/i;
  const postPattern = /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/i;
  const profilePathOf = (element) => {
    const url = safeUrl(element.getAttribute('href'));
    if (!url || !profilePattern.test(url.pathname)) return null;
    return normalizePath(url.pathname).toLowerCase();
  };
  const postPathOf = (element) => {
    const url = safeUrl(element.getAttribute('href'));
    if (!url || !postPattern.test(url.pathname)) return null;
    return normalizePath(url.pathname);
  };
  const configuredPath = '/@' + normalize(expectedUsername).replace(/^@/, '');
  const boundedText = (element) => {
    const role = normalize(element.getAttribute('role'));
    const control = ['A', 'BUTTON', 'INPUT', 'TEXTAREA'].includes(element.tagName) ||
      ['button', 'link', 'searchbox', 'tab', 'textbox'].includes(role);
    if (!control) return '';
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let text = '';
    for (let index = 0; index < 8; index += 1) {
      const node = walker.nextNode();
      if (!node) break;
      const value = node.nodeValue || '';
      if (value.length > 240 || text.length + value.length > 240) return '';
      text += ' ' + value;
    }
    return text;
  };
  const nameOf = (element) => {
    const candidates = [
      element.getAttribute('aria-label'),
      element.getAttribute('title'),
      element.getAttribute('placeholder'),
      element.getAttribute('alt'),
      boundedText(element)
    ];
    for (const candidate of candidates) {
      if (typeof candidate !== 'string' || candidate.length > 240) continue;
      const value = normalize(candidate);
      if (value) return value;
    }
    return '';
  };
  const labelOf = (element) => {
    if (element.tagName === 'A') {
      if (profilePathOf(element) === configuredPath) return 'PROFILE';
      if (profilePathOf(element) !== null || postPathOf(element) !== null) return 'OTHER';
    }
    const name = nameOf(element);
    if (name === 'home') return 'HOME';
    if (name === 'search' || name === 'search and explore') return 'SEARCH';
    if (name === 'profile' || name === 'your profile') return 'PROFILE';
    if (name === 'for you') return 'FOR_YOU';
    if (name === 'top') return 'TOP';
    if (name === 'recent') return 'RECENT';
    return 'OTHER';
  };
  const roleOf = (element) => {
    const explicit = normalize(element.getAttribute('role'));
    const roles = {
      button: 'BUTTON', link: 'LINK', navigation: 'NAVIGATION', main: 'MAIN',
      article: 'ARTICLE', searchbox: 'SEARCHBOX', textbox: 'TEXTBOX', tab: 'TAB',
      dialog: 'DIALOG', region: 'REGION', heading: 'HEADING', generic: 'GENERIC'
    };
    if (roles[explicit]) return roles[explicit];
    const tags = {
      A: 'LINK', BUTTON: 'BUTTON', NAV: 'NAVIGATION', MAIN: 'MAIN',
      ARTICLE: 'ARTICLE', INPUT: 'TEXTBOX', TEXTAREA: 'TEXTBOX', H1: 'HEADING',
      H2: 'HEADING', H3: 'HEADING', DIV: 'GENERIC', SECTION: 'GENERIC',
      HEADER: 'GENERIC', ASIDE: 'GENERIC', FORM: 'GENERIC'
    };
    return tags[element.tagName] || 'OTHER';
  };
  const tagOf = (element) => {
    return tagCategories.has(element.tagName) ? element.tagName : 'OTHER';
  };
  const tagCategories = new Set([
    'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'NAV', 'MAIN', 'ARTICLE', 'HEADER',
    'SECTION', 'ASIDE', 'DIV', 'FORM'
  ]);
  const navSelector = 'nav,[role="navigation"]';
  const candidateSelector = [
    'nav', '[role="navigation"]', 'main', '[role="main"]', 'header', 'article',
    '[role="article"]', 'aside', '[role="dialog"]', '[role="region"]', 'section',
    'form', 'button', '[role="button"]', 'a[href]', 'input', 'textarea',
    '[role="textbox"]', '[role="searchbox"]', '[role="tab"]'
  ].join(',');
  if (window.location.origin !== allowedOrigin) return {status: 'UNSAFE_ORIGIN'};
  if (window.location.href.length > 4_096) return {status: 'NODE_BOUND_EXCEEDED'};
  const url = new URL(window.location.href);
  const path = normalizePath(url.pathname);
  let routeClass = 'OTHER';
  if (postPattern.test(path)) routeClass = 'POST';
  else if (/\/(search|explore)(\/|$)/i.test(path) || url.searchParams.has('q')) routeClass = 'SEARCH';
  else if (path === '/' || path.toLowerCase() === '/home' || path.toLowerCase() === '/for_you') routeClass = 'HOME';

  const candidates = Array.from(document.querySelectorAll(candidateSelector));
  if (candidates.length > maxCandidates) return {status: 'NODE_BOUND_EXCEEDED'};
  const candidateVisibility = new Map(candidates.map((element) => [element, visibilityOf(element)]));
  const visibleCandidates = candidates.filter((element) => candidateVisibility.get(element) === 'VISIBLE');
  const offscreenCandidates = candidates.filter((element) => candidateVisibility.get(element) === 'OFFSCREEN');
  const clippedCandidates = candidates.filter((element) => candidateVisibility.get(element) === 'CLIPPED');
  const descriptions = new Map();
  for (const element of visibleCandidates) {
    descriptions.set(element, {
      role: roleOf(element), tag: tagOf(element), label: labelOf(element)
    });
  }
  const visibleAnchors = visibleCandidates.filter((element) => element.tagName === 'A');
  const profileLinks = visibleAnchors.filter((element) => profilePathOf(element) !== null);
  const selfProfileLinks = profileLinks.filter((element) => profilePathOf(element) === configuredPath);
  const otherProfileLinks = profileLinks.filter((element) => profilePathOf(element) !== configuredPath);
  const postLinks = visibleAnchors.filter((element) => postPathOf(element) !== null);
  const searchboxes = visibleCandidates.filter((element) => {
    const role = roleOf(element);
    const isInput = element.tagName === 'INPUT' || element.tagName === 'TEXTAREA';
    return role === 'SEARCHBOX' || role === 'TEXTBOX' && isInput &&
      (descriptions.get(element).label === 'SEARCH' || normalize(element.getAttribute('type')) === 'search');
  });
  const queryValue = normalize(configuredTopic);
  const queryMatch = queryValue.length > 0 && (
    searchboxes.some((element) => normalize(element.value) === queryValue) ||
    normalize(url.searchParams.get('q')) === queryValue
  );
  const searchControls = visibleCandidates.filter((element) => descriptions.get(element).label === 'SEARCH');
  const profileControls = visibleCandidates.filter((element) => descriptions.get(element).label === 'PROFILE');
  const homeControls = visibleCandidates.filter((element) =>
    ['HOME', 'FOR_YOU', 'TOP', 'RECENT'].includes(descriptions.get(element).label)
  );

  const panelFor = new Map();
  for (const searchbox of searchboxes) {
    let parent = searchbox.parentElement;
    for (let depth = 0; parent && depth < 12; depth += 1, parent = parent.parentElement) {
      const role = normalize(parent.getAttribute('role'));
      if (['dialog', 'region'].includes(role) ||
          ['DIV', 'SECTION', 'ASIDE', 'FORM'].includes(parent.tagName)) {
        if (visible(parent)) panelFor.set(searchbox, parent);
        break;
      }
    }
  }

  const selected = new Set();
  for (const element of visibleCandidates) {
    const info = descriptions.get(element);
    const landmark = ['NAVIGATION', 'MAIN', 'ARTICLE'].includes(info.role) ||
      element.tagName === 'HEADER' || element.tagName === 'ASIDE' ||
      element.getAttribute('role') === 'dialog' || element.getAttribute('role') === 'region';
    const control = ['A', 'BUTTON', 'INPUT', 'TEXTAREA'].includes(element.tagName) ||
      ['BUTTON', 'LINK', 'SEARCHBOX', 'TEXTBOX', 'TAB'].includes(info.role);
    const inNavigation = element.closest(navSelector) !== null;
    const semanticLink = element.tagName === 'A' &&
      (profilePathOf(element) !== null || postPathOf(element) !== null);
    if (landmark || searchboxes.includes(element) || semanticLink ||
        control && (info.label !== 'OTHER' || inNavigation)) selected.add(element);
  }
  for (const panel of panelFor.values()) selected.add(panel);
  if (selected.size > maxNodes) return {status: 'NODE_BOUND_EXCEEDED'};

  const elements = Array.from(selected);
  const ids = new Map(elements.map((element, index) => [element, index]));
  const nodes = elements.map((element, id) => {
    const info = descriptions.get(element) || {
      role: roleOf(element), tag: tagOf(element), label: labelOf(element)
    };
    return {id, role: info.role, tag: info.tag, label: info.label};
  });
  const relations = [];
  const relationKeys = new Set();
  let relationOverflow = false;
  const addRelation = (source, target, relation) => {
    if (relations.length >= maxRelations) {
      relationOverflow = true;
      return;
    }
    if (!ids.has(source) || !ids.has(target)) return;
    const sourceId = ids.get(source);
    const targetId = ids.get(target);
    const key = sourceId + ':' + targetId + ':' + relation;
    if (relationKeys.has(key)) return;
    relationKeys.add(key);
    relations.push({source: sourceId, target: targetId, relation});
  };
  for (const element of selected) {
    const info = descriptions.get(element) || {label: 'OTHER'};
    const nav = element.closest(navSelector);
    if (nav && nav !== element && ['HOME', 'SEARCH', 'PROFILE', 'FOR_YOU', 'TOP', 'RECENT'].includes(info.label)) {
      addRelation(nav, element, 'CONTROL_DESCENDANT_OF_NAV');
    }
    if (element.tagName === 'A' && profilePathOf(element) === configuredPath && info.label === 'PROFILE') {
      addRelation(element, element, 'CONTROL_SAME_NODE_AS_SELF_LINK');
    }
  }
  for (const [searchbox, panel] of panelFor.entries()) {
    addRelation(panel, searchbox, 'PANEL_CONTAINS_SEARCHBOX');
  }
  if (relationOverflow) return {status: 'NODE_BOUND_EXCEEDED'};

  const authorGroups = new Map();
  for (const postLink of postLinks) {
    let ancestor = postLink.parentElement;
    let found = null;
    for (let depth = 0; ancestor && depth < 12; depth += 1, ancestor = ancestor.parentElement) {
      if (['BODY', 'HTML'].includes(ancestor.tagName)) break;
      if (['HEADER', 'NAV', 'ASIDE', 'FOOTER'].includes(ancestor.tagName)) continue;
      const nestedPostLinks = Array.from(ancestor.querySelectorAll('a[href]')).filter(
        (element) => visible(element) && postPathOf(element) !== null
      );
      if (nestedPostLinks.length !== 1 || nestedPostLinks[0] !== postLink) continue;
      const nestedAuthors = Array.from(ancestor.querySelectorAll('a[href]')).filter(
        (element) => visible(element) && profilePathOf(element) !== null
      );
      if (nestedAuthors.length > 0) {
        found = {container: ancestor, authors: nestedAuthors};
        break;
      }
    }
    if (found) authorGroups.set(postLink, found);
  }
  const currentPostLinks = postLinks.filter((element) => postPathOf(element) === path);
  const clickedPostMatchesDestination = typeof operatorClickPath === 'string' &&
    /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}$/.test(operatorClickPath) &&
    url.search === '' && url.hash === '' && path === operatorClickPath;
  const clickedPostAuthorPath = typeof operatorClickPath === 'string'
    ? operatorClickPath.replace(/\/post\/[A-Za-z0-9_-]{1,120}$/, '').toLowerCase()
    : '';
  const clickedPostAuthorMatches = clickedPostMatchesDestination
    ? Array.from(authorGroups.entries()).filter(([postLink, group]) => {
        const authors = group.authors.filter((author) => profilePathOf(author) !== null);
        return postPathOf(postLink) === operatorClickPath && authors.length === 1 &&
          profilePathOf(authors[0]) === clickedPostAuthorPath;
      })
    : [];
  const searchQueryEntered = searchboxes.some((element) => normalize(element.value) === queryValue);
  const searchQueryApplied = normalize(url.searchParams.get('q')) === queryValue;
  let bodyStatus = 'UNKNOWN';
  for (const [postLink, group] of authorGroups.entries()) {
    const isDetail = routeClass === 'POST' && currentPostLinks.length === 1 && postLink === currentPostLinks[0];
    for (const authorLink of group.authors) {
      addRelation(
        postLink,
        authorLink,
        isDetail ? 'DETAIL_POST_AND_AUTHOR_SHARE_CONTAINER' :
          'RESULT_POST_AND_AUTHOR_SHARE_CONTAINER'
      );
      if (relationOverflow) break;
    }
    if (relationOverflow) break;
    if (isDetail) {
      const bodyParagraphs = Array.from(group.container.querySelectorAll('p,[role="paragraph"]'));
      if (bodyParagraphs.length > maxCandidates) return {status: 'NODE_BOUND_EXCEEDED'};
      const hasBodyText = bodyParagraphs.some((element) => {
        if (!visible(element)) return false;
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        for (let index = 0; index < 32; index += 1) {
          const node = walker.nextNode();
          if (!node) return false;
          const value = node.nodeValue || '';
          if (/\S/.test(value.slice(0, 240))) return true;
        }
        return false;
      });
      const moreControl = Array.from(group.container.querySelectorAll('button,a,[role="button"]'))
        .some((element) => visible(element) && ['see more', 'show more', 'read more'].includes(nameOf(element)));
      if (moreControl) bodyStatus = 'PARTIAL';
      else if (hasBodyText) bodyStatus = 'PRESENT';
      else if (group.container.querySelector('p,[role="paragraph"]')) bodyStatus = 'ABSENT';
    }
  }
  if (relationOverflow) return {status: 'NODE_BOUND_EXCEEDED'};

  const count = (elements, predicate) => elements.filter(predicate).length;
  const landmarks = visibleCandidates;
  const counts = {
    navigation_landmarks: bucket(count(landmarks, (element) => roleOf(element) === 'NAVIGATION')),
    main_landmarks: bucket(count(landmarks, (element) => roleOf(element) === 'MAIN')),
    header_landmarks: bucket(count(landmarks, (element) => element.tagName === 'HEADER')),
    article_landmarks: bucket(count(landmarks, (element) => roleOf(element) === 'ARTICLE')),
    search_controls: bucket(searchControls.length),
    profile_controls: bucket(profileControls.length),
    home_controls: bucket(homeControls.length),
    searchboxes: bucket(searchboxes.length),
    self_profile_links: bucket(selfProfileLinks.length),
    other_profile_links: bucket(otherProfileLinks.length),
    post_permalinks: bucket(postLinks.length),
    author_profile_links: bucket(profileLinks.length),
    panels: bucket(new Set(panelFor.values()).size),
    visible_candidates: bucket(visibleCandidates.length),
    offscreen_candidates: bucket(offscreenCandidates.length),
    clipped_candidates: bucket(clippedCandidates.length)
  };
  return {
    status: 'OBSERVED',
    route_class: routeClass,
    counts,
    nodes,
    relations,
    operator_action: operatorClick === null ? null : {
      source_route: operatorClick.source_route,
      role: operatorClick.role,
      tag: operatorClick.tag,
      label: operatorClick.label,
      within_unique_navigation: operatorClick.within_unique_navigation,
      is_post_permalink: operatorClick.is_post_permalink,
      destination_route: routeClass,
      destination_searchboxes: bucket(searchboxes.length),
      search_query_entered: searchQueryEntered,
      search_query_applied: searchQueryApplied,
      search_destination_relation: operatorClick.source_route === 'HOME' &&
        operatorClick.within_unique_navigation && ['HOME', 'SEARCH'].includes(routeClass) &&
        searchboxes.length === 1,
      clicked_post_matches_destination: clickedPostMatchesDestination,
      unique_co_container_author_matches_clicked_post: clickedPostAuthorMatches.length === 1
    },
    query_match: queryMatch,
    self_profile_match: selfProfileLinks.length > 0,
    body_status: bodyStatus,
    document_ready: document.readyState !== 'loading'
  };
}
"""


def validate_ui_diagnostic_snapshot(value: object) -> UiDiagnosticSnapshot:
    """Accept only the fixed symbolic DOM result; never preserve unknown fields."""

    if not isinstance(value, dict):
        raise UiDiagnosticSchemaError
    value_dict = cast(dict[str, object], value)
    status = value_dict.get("status")
    if status == "NODE_BOUND_EXCEEDED" and set(value_dict) == {"status"}:
        return UiDiagnosticSnapshot(status="NODE_BOUND_EXCEEDED")
    if status != "OBSERVED" or frozenset(value_dict) != _OBSERVED_KEYS:
        raise UiDiagnosticSchemaError

    route_class = value_dict.get("route_class")
    raw_counts = value_dict.get("counts")
    raw_nodes = value_dict.get("nodes")
    raw_relations = value_dict.get("relations")
    raw_operator_action = value_dict.get("operator_action")
    query_match = value_dict.get("query_match")
    self_profile_match = value_dict.get("self_profile_match")
    body_status = value_dict.get("body_status")
    document_ready = value_dict.get("document_ready")
    if not isinstance(raw_counts, dict):
        raise UiDiagnosticSchemaError
    counts_dict = cast(dict[str, object], raw_counts)
    if not isinstance(raw_nodes, list) or not isinstance(raw_relations, list):
        raise UiDiagnosticSchemaError
    nodes_list = cast(list[object], raw_nodes)
    relations_list = cast(list[object], raw_relations)
    if (
        not isinstance(route_class, str)
        or route_class not in _ROUTES
        or frozenset(counts_dict) != frozenset(_COUNT_KEYS)
        or len(nodes_list) > _MAX_DIAGNOSTIC_NODES
        or len(relations_list) > _MAX_DIAGNOSTIC_RELATIONS
        or type(query_match) is not bool
        or type(self_profile_match) is not bool
        or not isinstance(body_status, str)
        or body_status not in _BODY_STATES
        or type(document_ready) is not bool
    ):
        raise UiDiagnosticSchemaError

    counts: list[tuple[str, str]] = []
    for key in _COUNT_KEYS:
        count = counts_dict[key]
        if not isinstance(count, str) or count not in _CARDINALITIES:
            raise UiDiagnosticSchemaError
        counts.append((key, count))

    nodes: list[UiDiagnosticNode] = []
    for expected_id, raw_node in enumerate(nodes_list):
        if not isinstance(raw_node, dict):
            raise UiDiagnosticSchemaError
        node_dict = cast(dict[str, object], raw_node)
        if set(node_dict) != {"id", "role", "tag", "label"}:
            raise UiDiagnosticSchemaError
        node_id = node_dict.get("id")
        role = node_dict.get("role")
        tag = node_dict.get("tag")
        label = node_dict.get("label")
        if (
            type(node_id) is not int
            or node_id != expected_id
            or not isinstance(role, str)
            or role not in _ROLES
            or not isinstance(tag, str)
            or tag not in _TAGS
            or not isinstance(label, str)
            or label not in _LABELS
        ):
            raise UiDiagnosticSchemaError
        nodes.append(UiDiagnosticNode(node_id, role, tag, label))

    relations: list[UiDiagnosticRelation] = []
    for raw_relation in relations_list:
        if not isinstance(raw_relation, dict):
            raise UiDiagnosticSchemaError
        relation_dict = cast(dict[str, object], raw_relation)
        if set(relation_dict) != {"source", "target", "relation"}:
            raise UiDiagnosticSchemaError
        source = relation_dict.get("source")
        target = relation_dict.get("target")
        relation = relation_dict.get("relation")
        if (
            type(source) is not int
            or source < 0
            or source >= len(nodes)
            or type(target) is not int
            or target < 0
            or target >= len(nodes)
            or not isinstance(relation, str)
            or relation not in _RELATIONS
        ):
            raise UiDiagnosticSchemaError
        relations.append(UiDiagnosticRelation(source, target, relation))

    operator_action: UiDiagnosticOperatorAction | None = None
    if raw_operator_action is not None:
        if not isinstance(raw_operator_action, dict):
            raise UiDiagnosticSchemaError
        action_dict = cast(dict[str, object], raw_operator_action)
        action_keys = frozenset(
            {
                "source_route",
                "role",
                "tag",
                "label",
                "within_unique_navigation",
                "is_post_permalink",
                "destination_route",
                "destination_searchboxes",
                "search_query_entered",
                "search_query_applied",
                "search_destination_relation",
                "clicked_post_matches_destination",
                "unique_co_container_author_matches_clicked_post",
            }
        )
        source_route = action_dict.get("source_route")
        role = action_dict.get("role")
        tag = action_dict.get("tag")
        label = action_dict.get("label")
        destination_route = action_dict.get("destination_route")
        destination_searchboxes = action_dict.get("destination_searchboxes")
        bool_fields = (
            "within_unique_navigation",
            "is_post_permalink",
            "search_query_entered",
            "search_query_applied",
            "search_destination_relation",
            "clicked_post_matches_destination",
            "unique_co_container_author_matches_clicked_post",
        )
        if (
            frozenset(action_dict) != action_keys
            or not isinstance(source_route, str)
            or source_route not in _ROUTES
            or not isinstance(role, str)
            or role not in _ROLES
            or not isinstance(tag, str)
            or tag not in _TAGS
            or not isinstance(label, str)
            or label not in _LABELS
            or not isinstance(destination_route, str)
            or destination_route not in _ROUTES
            or not isinstance(destination_searchboxes, str)
            or destination_searchboxes not in _CARDINALITIES
            or any(type(action_dict.get(field)) is not bool for field in bool_fields)
        ):
            raise UiDiagnosticSchemaError
        operator_action = UiDiagnosticOperatorAction(
            source_route=source_route,
            role=role,
            tag=tag,
            label=label,
            within_unique_navigation=cast(bool, action_dict["within_unique_navigation"]),
            is_post_permalink=cast(bool, action_dict["is_post_permalink"]),
            destination_route=destination_route,
            destination_searchboxes=destination_searchboxes,
            search_query_entered=cast(bool, action_dict["search_query_entered"]),
            search_query_applied=cast(bool, action_dict["search_query_applied"]),
            search_destination_relation=cast(bool, action_dict["search_destination_relation"]),
            clicked_post_matches_destination=cast(
                bool, action_dict["clicked_post_matches_destination"]
            ),
            unique_co_container_author_matches_clicked_post=cast(
                bool, action_dict["unique_co_container_author_matches_clicked_post"]
            ),
        )

    return UiDiagnosticSnapshot(
        status="OBSERVED",
        route_class=route_class,
        counts=tuple(counts),
        nodes=tuple(nodes),
        relations=tuple(relations),
        query_match=query_match,
        self_profile_match=self_profile_match,
        body_status=body_status,
        document_ready=document_ready,
        operator_action=operator_action,
    )
