"""Readers for local, explicitly synthetic HTML browser fixtures.

These readers consume an immutable in-memory document. They do not start a
browser, navigate, or perform network I/O. A future authorized browser reader
can implement the existing candidate protocols separately.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import SplitResult, parse_qsl, urlsplit
from uuid import UUID

from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserContractError,
    BrowserSessionMismatch,
    ChallengeDetected,
    LocatorNotFound,
    RemoteSessionStateUncertain,
    SessionExpired,
    UnsupportedUIState,
)
from threads_platform.browser.read_adapters import (
    BROWSER_SEARCH_CANDIDATE_BOUND,
    BROWSER_SEARCH_ITERATION_BOUND,
    normalize_manual_permalink,
    normalize_search_query,
)

_HTML_CHARACTER_BOUND = 1_000_000
_HTML_NODE_BOUND = 5_000
_THREAD_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_PROFILE_PATH = re.compile(r"/@([A-Za-z0-9._]{1,30})/?")
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_LOGIN_CHALLENGE_LABELS = frozenset(
    {"login challenge", "login required", "sign in required", "checkpoint required"}
)
_SESSION_LOSS_LABELS = frozenset({"session expired", "session unavailable", "logged out"})


def _new_html_content() -> list[str | _HtmlNode]:
    return []


@dataclass(frozen=True, slots=True)
class SyntheticHtmlDocument:
    """An in-memory fixture document with the URL the fixture represents."""

    url: str
    html: str


@dataclass(frozen=True, slots=True)
class SyntheticBrowserSessionContext:
    """Synthetic account scope used to prevent fixtures crossing accounts."""

    account_uuid: UUID


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    attributes: dict[str, str]
    parent: _HtmlNode | None = None
    content: list[str | _HtmlNode] = field(default_factory=_new_html_content)

    def descendants(self) -> Sequence[_HtmlNode]:
        result: list[_HtmlNode] = []
        for part in self.content:
            if isinstance(part, _HtmlNode):
                result.append(part)
                result.extend(part.descendants())
        return result

    def text_content(self) -> str:
        return "".join(
            part.text_content() if isinstance(part, _HtmlNode) else part for part in self.content
        )


class _FixtureHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("#document", {})
        self._stack = [self.root]
        self._node_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes: dict[str, str] = {}
        for name, value in attrs:
            normalized_name = name.casefold()
            if normalized_name in attributes:
                raise UnsupportedUIState()
            attributes[normalized_name] = value or ""
        self._node_count += 1
        if self._node_count > _HTML_NODE_BOUND or len(self._stack) > 128:
            raise UnsupportedUIState()
        node = _HtmlNode(tag.casefold(), attributes, self._stack[-1])
        self._stack[-1].content.append(node)
        if node.tag not in _VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        previous_depth = len(self._stack)
        self.handle_starttag(tag, attrs)
        if len(self._stack) > previous_depth:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) <= 1 or self._stack[-1].tag != tag.casefold():
            raise UnsupportedUIState()
        self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._stack[-1].tag not in {"script", "style"}:
            self._stack[-1].content.append(data)

    def close(self) -> None:
        super().close()
        if len(self._stack) != 1:
            raise UnsupportedUIState()


class SyntheticHtmlBrowserReader:
    """Expose bounded S2 candidates parsed from one local synthetic HTML page."""

    def __init__(
        self,
        document: SyntheticHtmlDocument,
        session_context: SyntheticBrowserSessionContext,
    ) -> None:
        self._document = document
        self._session_context = session_context

    async def search_candidates(
        self,
        query: str,
        *,
        iteration_bound: int,
        candidate_bound: int,
    ) -> Sequence[Sequence[Mapping[str, object]]]:
        if (
            iteration_bound != BROWSER_SEARCH_ITERATION_BOUND
            or candidate_bound != BROWSER_SEARCH_CANDIDATE_BOUND
        ):
            raise UnsupportedUIState()
        normalized_query = normalize_search_query(query)
        document_root = _prepare_fixture(self._document, self._session_context)
        _validate_search_url(self._document.url, normalized_query)
        main = _single_role(document_root, "main", "Search results")
        if main.attributes.get("data-query") != normalized_query:
            raise UnsupportedUIState()

        batches = _role_nodes(main, "region", "Search result batch")
        articles = _role_nodes(main, "article", None)
        if len(batches) > iteration_bound or len(articles) > candidate_bound:
            raise UnsupportedUIState()
        batch_articles = [
            article for batch in batches for article in _role_nodes(batch, "article", None)
        ]
        if (
            len(batch_articles) != len(articles)
            or len({id(item) for item in batch_articles}) != len(batch_articles)
        ) and (articles or batches):
            raise UnsupportedUIState()

        result_pages: list[tuple[Mapping[str, object], ...]] = []
        for batch in batches:
            result_pages.append(
                tuple(
                    _extract_post(article, include_parent=False)
                    for article in _role_nodes(batch, "article", None)
                )
            )
        return tuple(result_pages)

    async def read_conversation(
        self,
        target_ref: str,
        *,
        candidate_bound: int,
    ) -> tuple[Sequence[Mapping[str, object]], bool | None]:
        if candidate_bound != BROWSER_SEARCH_CANDIDATE_BOUND:
            raise UnsupportedUIState()
        try:
            exact_target = normalize_manual_permalink(target_ref).thread_ref
        except BrowserContractError:
            raise BrowserContractError() from None

        document_root = _prepare_fixture(self._document, self._session_context)
        _validate_conversation_url(self._document.url, exact_target)
        main = _single_role(document_root, "main", "Conversation")
        root_marker = main.attributes.get("data-target")
        if root_marker is None:
            raise LocatorNotFound()
        try:
            marked_target = normalize_manual_permalink(root_marker).thread_ref
        except BrowserContractError:
            raise UnsupportedUIState() from None
        if marked_target != exact_target:
            raise UnsupportedUIState()

        articles = _role_nodes(main, "article", None)
        if len(articles) > candidate_bound:
            raise UnsupportedUIState()
        candidates = tuple(_extract_post(article, include_parent=True) for article in articles)
        context_marker = main.attributes.get("data-context-status")
        context_complete = _context_completeness(context_marker)
        return candidates, context_complete


def _prepare_fixture(
    document: SyntheticHtmlDocument,
    session_context: SyntheticBrowserSessionContext,
) -> _HtmlNode:
    if not document.html or len(document.html) > _HTML_CHARACTER_BOUND:
        raise UnsupportedUIState()

    parser = _FixtureHtmlParser()
    try:
        parser.feed(document.html)
        parser.close()
    except UnsupportedUIState:
        raise
    except ValueError, AssertionError, RecursionError, TypeError:
        raise UnsupportedUIState() from None
    root = parser.root
    _detect_session_markers(root)

    account_markers = [
        node
        for node in _all_nodes(root)
        if node.tag == "meta"
        and node.attributes.get("name", "").casefold() == "synthetic-account-id"
    ]
    if len(account_markers) != 1:
        if account_markers:
            raise UnsupportedUIState()
        raise SessionExpired()
    try:
        fixture_account = UUID(account_markers[0].attributes.get("content", ""))
    except ValueError, AttributeError:
        raise SessionExpired() from None
    if fixture_account != session_context.account_uuid:
        raise BrowserSessionMismatch()
    return root


def _detect_session_markers(root: _HtmlNode) -> None:
    labels = {
        node.attributes.get("aria-label", "").strip().casefold()
        for node in _role_nodes(root, "alert", None)
    }
    if labels & _LOGIN_CHALLENGE_LABELS:
        raise ChallengeDetected()
    if labels & _SESSION_LOSS_LABELS:
        raise SessionExpired()


def _validate_search_url(url: str, query: str) -> None:
    parsed = _expected_origin_url(url)
    if parsed.path != "/search" or parsed.fragment:
        raise RemoteSessionStateUncertain()
    try:
        params = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=1,
        )
    except ValueError:
        raise RemoteSessionStateUncertain() from None
    if params != [("q", query)]:
        raise RemoteSessionStateUncertain()


def _validate_conversation_url(url: str, exact_target: str) -> None:
    try:
        page_target = normalize_manual_permalink(url).thread_ref
    except BrowserContractError:
        raise RemoteSessionStateUncertain() from None
    if page_target != exact_target:
        raise RemoteSessionStateUncertain()


def _expected_origin_url(url: str) -> SplitResult:
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise RemoteSessionStateUncertain() from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.netloc.casefold() != BROWSER_FEED_ORIGIN.removeprefix("https://").casefold()
    ):
        raise RemoteSessionStateUncertain()
    return parsed


def _single_role(root: _HtmlNode, role: str, label: str) -> _HtmlNode:
    nodes = _role_nodes(root, role, label)
    if not nodes:
        raise LocatorNotFound()
    if len(nodes) != 1:
        raise UnsupportedUIState()
    return nodes[0]


def _role_nodes(root: _HtmlNode, role: str, label: str | None) -> list[_HtmlNode]:
    return [
        node
        for node in _all_nodes(root)
        if node.attributes.get("role") == role
        and (label is None or node.attributes.get("aria-label") == label)
        and _is_accessible(node)
    ]


def _all_nodes(root: _HtmlNode) -> Sequence[_HtmlNode]:
    return (root, *root.descendants())


def _is_accessible(node: _HtmlNode) -> bool:
    current: _HtmlNode | None = node
    while current is not None:
        if current.attributes.get("aria-hidden", "").casefold() == "true":
            return False
        current = current.parent
    return True


def _extract_post(article: _HtmlNode, *, include_parent: bool) -> Mapping[str, object]:
    permalink = _single_role(article, "link", "Post permalink")
    permalink_href = permalink.attributes.get("href")
    if not permalink_href:
        raise LocatorNotFound()

    author_marker = _single_role(article, "link", "Author")
    author_text = author_marker.text_content().strip()
    if not author_text.startswith("@"):
        raise UnsupportedUIState()
    author_username = author_text[1:]
    if _THREAD_USERNAME.fullmatch(author_username) is None:
        raise UnsupportedUIState()
    author_href = author_marker.attributes.get("href")
    if author_href is None or _profile_username(author_href) != author_username.casefold():
        raise UnsupportedUIState()

    candidate: dict[str, object] = {
        "href": permalink_href,
        "author_username": author_username,
    }
    text_markers = _role_nodes(article, "paragraph", "Post text")
    if len(text_markers) > 1:
        raise UnsupportedUIState()
    content_truncated = False
    if text_markers:
        text_marker = text_markers[0]
        candidate["text_excerpt"] = text_marker.text_content()
        content_truncated = _boolean_marker(text_marker.attributes.get("data-truncated"))
    candidate["content_truncated"] = content_truncated

    parent_markers = _role_nodes(article, "link", "Replying to")
    if len(parent_markers) > 1:
        raise UnsupportedUIState()
    if not include_parent and parent_markers:
        raise UnsupportedUIState()
    if include_parent and parent_markers:
        parent_href = parent_markers[0].attributes.get("href")
        if not parent_href:
            raise UnsupportedUIState()
        candidate["parent_ref"] = parent_href
    elif include_parent:
        candidate["parent_ref"] = None
    return candidate


def _profile_username(href: str) -> str | None:
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme:
        if (
            parsed.scheme.casefold() != "https"
            or parsed.netloc.casefold() != BROWSER_FEED_ORIGIN.removeprefix("https://").casefold()
        ):
            return None
    elif parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    if parsed.query or parsed.fragment or "%" in parsed.path or "\\" in parsed.path:
        return None
    match = _PROFILE_PATH.fullmatch(parsed.path)
    return match.group(1).casefold() if match else None


def _boolean_marker(value: str | None) -> bool:
    if value is None or value.casefold() == "false":
        return False
    if value.casefold() == "true":
        return True
    raise UnsupportedUIState()


def _context_completeness(value: str | None) -> bool | None:
    if value is None or value.casefold() == "unknown":
        return None
    if value.casefold() == "complete":
        return True
    if value.casefold() == "partial":
        return False
    raise UnsupportedUIState()
