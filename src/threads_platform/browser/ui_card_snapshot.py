"""Bounded snapshots of explicitly marked synthetic Browser Jobs DOM.

The selectors in this module describe the local fixture contract. They are not
selectors for Threads and cannot establish live author identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page

from threads_platform.browser.job_observation import FakeBodyVisibility, ObservedPostKind

MAX_TRANCHE_COUNT = 5
MAX_CARDS_PER_TRANCHE = 20
MAX_POST_BODY_CODEPOINTS = 10_000
MAX_AUTHOR_CODEPOINTS = 128
_SYNTHETIC_POST_PATH_RE = re.compile(
    r"^/@synthetic-[A-Za-z0-9][A-Za-z0-9._-]{0,62}/[A-Za-z0-9._:-]{1,128}$"
)


class UiCardSnapshotError(ValueError):
    """Fixed-code failure for an ambiguous or unsafe synthetic DOM snapshot."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class FeedCardSnapshot:
    local_card_id: str
    canonical_post_url: str
    author_label: str

    def __repr__(self) -> str:
        return "FeedCardSnapshot(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class FeedTrancheSnapshot:
    batch_index: int
    cards: tuple[FeedCardSnapshot, ...]

    def __repr__(self) -> str:
        return f"FeedTrancheSnapshot(batch_index={self.batch_index}, card_count={len(self.cards)})"


@dataclass(frozen=True, slots=True, repr=False)
class PostDetailSnapshot:
    canonical_post_url: str
    author_label: str
    body: str | None
    body_visibility: FakeBodyVisibility
    post_kind: ObservedPostKind
    body_truncated: bool
    collapsed_content: bool

    def __repr__(self) -> str:
        return (
            "PostDetailSnapshot("
            f"body_visibility={self.body_visibility.value!r}, "
            f"post_kind={self.post_kind.value!r}, "
            f"body_codepoints={len(self.body) if self.body is not None else 0})"
        )


def snapshot_search_feed(page: Page, *, query: str) -> tuple[FeedTrancheSnapshot, ...]:
    """Read indexed visible feed tranches, scrolling the rendered page between them."""

    if not query or len(query) > 120 or any(ord(character) < 32 for character in query):
        raise UiCardSnapshotError("SEARCH_QUERY_INVALID")
    main = page.locator("main[data-search-query]")
    if main.count() != 1 or not main.is_visible():
        raise UiCardSnapshotError("SEARCH_ROOT_INVALID")
    if main.get_attribute("data-search-query") != query:
        raise UiCardSnapshotError("SEARCH_QUERY_MISMATCH")

    tranches = main.locator(":scope > section[data-feed-tranche]")
    tranche_count = tranches.count()
    if not 2 <= tranche_count <= MAX_TRANCHE_COUNT:
        raise UiCardSnapshotError("TRANCHE_COUNT_INVALID")
    viewport = page.viewport_size
    if viewport is None:
        raise UiCardSnapshotError("VIEWPORT_UNAVAILABLE")

    snapshots: list[FeedTrancheSnapshot] = []
    for batch_index in range(tranche_count):
        tranche = tranches.nth(batch_index)
        if tranche.get_attribute("data-feed-tranche") != str(batch_index):
            raise UiCardSnapshotError("TRANCHE_ORDER_INVALID")
        if not tranche.is_visible():
            raise UiCardSnapshotError("TRANCHE_HIDDEN")
        if batch_index:
            before = tranche.bounding_box()
            if before is None or before["y"] < viewport["height"]:
                raise UiCardSnapshotError("TRANCHE_SCROLL_NOT_REQUIRED")
            tranche.scroll_into_view_if_needed(timeout=1_000)
            after = tranche.bounding_box()
            if (
                after is None
                or abs(before["y"] - after["y"]) < 1
                or after["y"] >= viewport["height"]
                or after["y"] + after["height"] <= 0
            ):
                raise UiCardSnapshotError("TRANCHE_SCROLL_FAILED")

        card_locators = tranche.locator(":scope > article[data-post-card]")
        card_count = card_locators.count()
        if card_count > MAX_CARDS_PER_TRANCHE:
            raise UiCardSnapshotError("TRANCHE_CARD_LIMIT_EXCEEDED")
        cards: list[FeedCardSnapshot] = []
        for card_index in range(card_count):
            card = card_locators.nth(card_index)
            if not card.is_visible():
                continue
            canonical_url = _read_permalink(card)
            author = _read_author(card)
            _validate_author_url_binding(author, canonical_url)
            cards.append(
                FeedCardSnapshot(
                    local_card_id=f"dom-{batch_index + 1:02d}-{card_index + 1:03d}",
                    canonical_post_url=canonical_url,
                    author_label=author,
                )
            )
        if not cards:
            raise UiCardSnapshotError("TRANCHE_EMPTY")
        snapshots.append(FeedTrancheSnapshot(batch_index=batch_index, cards=tuple(cards)))
    return tuple(snapshots)


def snapshot_exact_post_detail(page: Page, *, requested_url: str) -> PostDetailSnapshot:
    """Read the permalink, own author, and own body from one exact root article."""

    if not is_reserved_synthetic_post_url(requested_url):
        raise UiCardSnapshotError("DETAIL_TARGET_INVALID")
    main = page.locator("main[data-post-detail]")
    if main.count() != 1 or not main.is_visible():
        raise UiCardSnapshotError("DETAIL_ROOT_INVALID")
    root_posts = main.locator(":scope > article[data-root-post]")
    if root_posts.count() != 1 or not root_posts.is_visible():
        raise UiCardSnapshotError("DETAIL_POST_INVALID")
    root_post = root_posts.nth(0)
    canonical_url = _read_permalink(root_post)
    author = _read_author(root_post)
    _validate_author_url_binding(author, canonical_url)
    if canonical_url != requested_url:
        raise UiCardSnapshotError("DETAIL_TARGET_CHANGED")

    bodies = root_post.locator(":scope > [data-own-post-body]")
    body: str | None = None
    visibility = FakeBodyVisibility.UNKNOWN
    body_truncated = False
    collapsed_content = False
    if bodies.count() == 1:
        own_body = bodies.nth(0)
        visibility_value = own_body.get_attribute("data-body-visibility")
        try:
            visibility = FakeBodyVisibility(visibility_value or "UNKNOWN")
        except ValueError:
            visibility = FakeBodyVisibility.UNKNOWN
        collapsed_value = own_body.get_attribute("data-collapsed")
        truncated_value = own_body.get_attribute("data-truncated")
        if collapsed_value not in (None, "true", "false") or truncated_value not in (
            None,
            "true",
            "false",
        ):
            raise UiCardSnapshotError("DETAIL_BODY_STATE_INVALID")
        collapsed_content = collapsed_value == "true"
        body_truncated = truncated_value == "true"
        has_ambiguous_content = (
            own_body.locator(
                "blockquote, article, [data-quoted-post], [data-parent-post], "
                '[aria-hidden="true"], [inert]'
            ).count()
            > 0
        )
        if own_body.is_visible() and not has_ambiguous_content:
            body = own_body.inner_text().strip()
            if len(body) > MAX_POST_BODY_CODEPOINTS:
                raise UiCardSnapshotError("DETAIL_BODY_LIMIT_EXCEEDED")
        else:
            visibility = FakeBodyVisibility.UNKNOWN
        if not body:
            body = None
            visibility = FakeBodyVisibility.UNKNOWN

    post_kind_value = root_post.get_attribute("data-post-kind") or "UNKNOWN"
    try:
        post_kind = ObservedPostKind(post_kind_value)
    except ValueError:
        post_kind = ObservedPostKind.UNKNOWN
    if post_kind is not ObservedPostKind.ORIGINAL:
        visibility = FakeBodyVisibility.PARTIAL
        body_truncated = True
    if collapsed_content or body_truncated:
        visibility = FakeBodyVisibility.PARTIAL

    return PostDetailSnapshot(
        canonical_post_url=canonical_url,
        author_label=author,
        body=body,
        body_visibility=visibility,
        post_kind=post_kind,
        body_truncated=body_truncated,
        collapsed_content=collapsed_content,
    )


def _read_permalink(root: Locator) -> str:
    links = root.locator(":scope > a[data-post-permalink]")
    if links.count() != 1 or not links.is_visible():
        raise UiCardSnapshotError("POST_PERMALINK_INVALID")
    value = links.get_attribute("href")
    if not is_reserved_synthetic_post_url(value):
        raise UiCardSnapshotError("POST_PERMALINK_INVALID")
    assert isinstance(value, str)
    return value


def _read_author(root: Locator) -> str:
    authors = root.locator(":scope > header[data-post-header] [data-post-author]")
    if authors.count() != 1 or not authors.is_visible():
        raise UiCardSnapshotError("POST_AUTHOR_INVALID")
    value = " ".join(authors.inner_text().split())
    if len(value) > MAX_AUTHOR_CODEPOINTS or not value.startswith("@synthetic-"):
        raise UiCardSnapshotError("POST_AUTHOR_INVALID")
    return value


def _validate_author_url_binding(author: str, canonical_url: str) -> None:
    try:
        handle = urlsplit(canonical_url).path.split("/", maxsplit=2)[1]
    except IndexError, ValueError:
        raise UiCardSnapshotError("POST_IDENTITY_INVALID") from None
    if handle != author:
        raise UiCardSnapshotError("POST_IDENTITY_INVALID")


def is_reserved_synthetic_post_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2_048 or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "threads.example.invalid"
        and parsed.hostname == "threads.example.invalid"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and _SYNTHETIC_POST_PATH_RE.fullmatch(parsed.path) is not None
        and not parsed.query
        and not parsed.fragment
    )
