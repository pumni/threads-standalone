"""Regression coverage for conservative synthetic Browser Jobs DOM snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Error, Page, sync_playwright

from threads_platform.browser.job_observation import FakeBodyVisibility, ObservedPostKind
from threads_platform.browser.ui_card_snapshot import (
    UiCardSnapshotError,
    snapshot_exact_post_detail,
    snapshot_search_feed,
)


@pytest.fixture
def chromium_page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        try:
            browser: Browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-background-networking", "--disable-sync", "--no-first-run"],
            )
        except Error:
            pytest.skip("Playwright Chromium cannot launch in this environment")
        context = browser.new_context(
            java_script_enabled=False, viewport={"width": 800, "height": 600}
        )
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        try:
            yield page
        finally:
            browser.close()


def _card(url: str, author: str) -> str:
    return (
        "<article data-post-card>"
        f'<a data-post-permalink href="{url}">open</a>'
        f"<header data-post-header><span data-post-author>{author}</span></header>"
        "</article>"
    )


def test_feed_snapshot_scrolls_distinct_tranches_and_keeps_duplicate_candidates(
    chromium_page: Page,
) -> None:
    first = "https://threads.example.invalid/@synthetic-shop/post-001"
    second = "https://threads.example.invalid/@synthetic-seeker/post-002"
    first_card = _card(first, "@synthetic-shop")
    sidebar_card = _card(
        "https://threads.example.invalid/@synthetic-sidebar/post-900", "@synthetic-sidebar"
    )
    chromium_page.set_content(
        f"""
        <main data-search-query="synthetic hiring">
          <section data-feed-tranche="0" style="height: 900px">
            {first_card}
            <aside>{sidebar_card}</aside>
          </section>
          <section data-feed-tranche="1" style="height: 900px">
            {first_card}
            {_card(second, "@synthetic-seeker")}
          </section>
        </main>
        """
    )

    snapshots = snapshot_search_feed(chromium_page, query="synthetic hiring")

    assert len(snapshots) == 2
    assert [len(item.cards) for item in snapshots] == [1, 2]
    assert snapshots[0].cards[0].canonical_post_url == first
    assert snapshots[1].cards[0].canonical_post_url == first
    assert snapshots[1].cards[1].canonical_post_url == second
    after_scroll = chromium_page.locator('section[data-feed-tranche="1"]').bounding_box()
    assert after_scroll is not None
    assert after_scroll["y"] < 600
    assert after_scroll["y"] + after_scroll["height"] > 0


def test_feed_snapshot_rejects_author_detached_from_its_permalink(chromium_page: Page) -> None:
    url = "https://threads.example.invalid/@synthetic-root/post-001"
    chromium_page.set_content(
        f"""
        <main data-search-query="synthetic hiring">
          <section data-feed-tranche="0" style="height: 900px">{_card(url, "@synthetic-other")}</section>
          <section data-feed-tranche="1" style="height: 900px">{_card(url, "@synthetic-other")}</section>
        </main>
        """
    )

    with pytest.raises(UiCardSnapshotError) as error:
        snapshot_search_feed(chromium_page, query="synthetic hiring")

    assert error.value.code == "POST_IDENTITY_INVALID"


def test_detail_snapshot_uses_one_root_and_excludes_hidden_and_quoted_text(
    chromium_page: Page,
) -> None:
    target = "https://threads.example.invalid/@synthetic-sharer/post-003"
    other = "https://threads.example.invalid/@synthetic-sidebar/post-900"
    chromium_page.set_content(
        f"""
        <main data-post-detail>
          <aside><a data-post-permalink href="{other}">sidebar</a>
            <header data-post-header><span data-post-author>@synthetic-sidebar</span></header>
          </aside>
          <article data-root-post data-post-kind="QUOTE">
            <a data-post-permalink href="{target}">root permalink</a>
            <header data-post-header><span data-post-author>@synthetic-sharer</span></header>
            <div data-own-post-body data-body-visibility="FULL_VISIBLE">
              Mình chia sẻ bài này.
              <span hidden>Cần tuyển 99 nhân viên lương 99 triệu.</span>
            </div>
            <aside data-quoted-post>
              <blockquote>Cần tuyển nhân viên bán hàng tại Hà Nội.</blockquote>
            </aside>
          </article>
        </main>
        """
    )

    snapshot = snapshot_exact_post_detail(chromium_page, requested_url=target)

    assert snapshot.canonical_post_url == target
    assert snapshot.author_label == "@synthetic-sharer"
    assert snapshot.body == "Mình chia sẻ bài này."
    assert snapshot.body_visibility is FakeBodyVisibility.PARTIAL
    assert snapshot.post_kind is ObservedPostKind.QUOTE
    assert snapshot.body_truncated


def test_detail_snapshot_rejects_a_permalink_changed_from_requested_target(
    chromium_page: Page,
) -> None:
    target = "https://threads.example.invalid/@synthetic-root/post-001"
    changed = "https://threads.example.invalid/@synthetic-root/post-002"
    chromium_page.set_content(
        f"""
        <main data-post-detail>
          <article data-root-post data-post-kind="ORIGINAL">
            <a data-post-permalink href="{changed}">root permalink</a>
            <header data-post-header><span data-post-author>@synthetic-root</span></header>
            <div data-own-post-body data-body-visibility="FULL_VISIBLE">body</div>
          </article>
        </main>
        """
    )

    with pytest.raises(UiCardSnapshotError) as error:
        snapshot_exact_post_detail(chromium_page, requested_url=target)

    assert error.value.code == "DETAIL_TARGET_CHANGED"
