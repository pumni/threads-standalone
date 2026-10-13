"""Offline Chromium adapter from synthetic HTML to the existing BJ-02 fake seam."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, sync_playwright

from threads_platform.browser.job_observation import (
    SYNTHETIC_SEARCH_URL,
    FakeJobCardV1,
    FakeJobFeedBatchV1,
    FakeJobPostDetailV1,
    FakePageState,
)
from threads_platform.browser.ui_card_snapshot import (
    FeedTrancheSnapshot,
    PostDetailSnapshot,
    UiCardSnapshotError,
    is_reserved_synthetic_post_url,
    snapshot_exact_post_detail,
    snapshot_search_feed,
)
from threads_platform.standalone.browser_job_watch import SyntheticCycleInput

MAX_DOM_CYCLES = 5
MAX_DOM_BATCHES = 5
MAX_DOM_CANDIDATES = 100
MAX_DOM_DETAILS = 20
MAX_DOM_MANIFEST_BYTES = 1_100_000
MAX_DOM_CAPTURE_SECONDS = 30.0
MAX_BROWSER_OPERATION_MS = 1_000
MAX_BROWSER_START_MS = 5_000


class SyntheticDomFixtureError(ValueError):
    """Fixed-code validation or local-render failure; values are never included."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class _DomDetailFixture:
    target: str = field(repr=False)
    html: str = field(repr=False)

    def __repr__(self) -> str:
        return "_DomDetailFixture(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _DomCycleFixture:
    feed_html: str = field(repr=False)
    details: tuple[_DomDetailFixture, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"_DomCycleFixture(detail_count={len(self.details)})"


@dataclass(frozen=True, slots=True, repr=False)
class _DomManifest:
    query: str = field(repr=False)
    cycles: tuple[_DomCycleFixture, ...] = field(repr=False)
    protocol_query: str = field(repr=False)

    def __repr__(self) -> str:
        return f"_DomManifest(cycle_count={len(self.cycles)})"


@dataclass(frozen=True, slots=True, repr=False)
class DomFixtureCapture:
    """Immutable pre-snapshot summary and typed fake sessions for BJ-04A."""

    protocol_query: str = field(repr=False)
    cycles: tuple[SyntheticCycleInput, ...] = field(repr=False)
    batch_count: int
    candidate_count: int
    target_count: int
    duplicate_count: int
    detail_count: int
    blocked_request_count: int
    visible_texts: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "DomFixtureCapture("
            f"cycles={len(self.cycles)}, batches={self.batch_count}, "
            f"candidates={self.candidate_count}, targets={self.target_count}, "
            f"duplicates={self.duplicate_count}, details={self.detail_count}, "
            f"blocked_requests={self.blocked_request_count})"
        )


class _DomFixtureSession:
    """Bounded in-memory feed batches/details; no navigation or external I/O."""

    def __init__(
        self,
        *,
        batches: tuple[FakeJobFeedBatchV1, ...],
        details: dict[str, FakeJobPostDetailV1],
        account_uuid: UUID,
        query: str,
    ) -> None:
        self._batches = batches
        self._details = details
        self._account_uuid = account_uuid
        self._query = query
        self._initial_read = False
        self._next_batch_index = 1

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        if (
            self._initial_read
            or account_uuid != self._account_uuid
            or query != self._query
            or max_cards < len(self._batches[0].cards)
        ):
            raise ValueError("DOM_SESSION_MISMATCH")
        self._initial_read = True
        return self._batches[0]

    def scroll_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        next_batch_index: int,
        increment: int,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        if (
            account_uuid != self._account_uuid
            or query != self._query
            or next_batch_index != self._next_batch_index
            or increment != 1
            or not 0 <= next_batch_index < len(self._batches)
            or max_cards < len(self._batches[next_batch_index].cards)
        ):
            raise ValueError("DOM_SESSION_MISMATCH")
        self._next_batch_index += 1
        return self._batches[next_batch_index]

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> FakeJobPostDetailV1 | None:
        if account_uuid != self._account_uuid:
            raise ValueError("DOM_SESSION_MISMATCH")
        return self._details.get(canonical_post_url)


def capture_synthetic_dom_fixture(
    document: object,
    *,
    account_uuid: UUID,
    max_cycles: int,
    max_posts_per_cycle: int,
) -> DomFixtureCapture:
    """Render local fixture documents in ephemeral Chromium and adapt snapshots to BJ-02."""

    manifest = _decode_manifest(document)
    if type(max_cycles) is not int or not 1 <= max_cycles <= MAX_DOM_CYCLES:
        raise SyntheticDomFixtureError("WATCH_POLICY_INVALID")
    if type(max_posts_per_cycle) is not int or not 1 <= max_posts_per_cycle <= MAX_DOM_DETAILS:
        raise SyntheticDomFixtureError("WATCH_POLICY_INVALID")
    if len(manifest.cycles) < max_cycles:
        raise SyntheticDomFixtureError("DOM_CYCLE_COUNT_INVALID")

    deadline = time.monotonic() + MAX_DOM_CAPTURE_SECONDS
    batch_count = 0
    candidate_count = 0
    target_count = 0
    duplicate_count = 0
    detail_count = 0
    blocked_request_count = 0
    cycles: list[SyntheticCycleInput] = []
    visible_texts: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                timeout=MAX_BROWSER_START_MS,
                args=["--disable-background-networking", "--disable-sync", "--no-first-run"],
            )
            try:
                context = browser.new_context(
                    accept_downloads=False,
                    java_script_enabled=False,
                    service_workers="block",
                    viewport={"width": 800, "height": 600},
                )

                def abort_request(route: Route) -> None:
                    nonlocal blocked_request_count
                    blocked_request_count += 1
                    route.abort()

                context.route("**/*", abort_request)
                page = context.new_page()
                page.set_default_timeout(MAX_BROWSER_OPERATION_MS)

                for cycle_fixture in manifest.cycles[:max_cycles]:
                    _require_time(deadline)
                    page.set_content(
                        cycle_fixture.feed_html,
                        wait_until="domcontentloaded",
                        timeout=_remaining_timeout_ms(deadline),
                    )
                    tranches = snapshot_search_feed(page, query=manifest.query)
                    cycle_cards, cycle_targets, cycle_duplicates = _feed_cards(
                        tranches,
                        account_uuid=account_uuid,
                    )
                    if len(cycle_targets) > max_posts_per_cycle:
                        raise SyntheticDomFixtureError("DOM_DETAIL_LIMIT_EXCEEDED")
                    if (
                        candidate_count + sum(len(item.cards) for item in tranches)
                        > MAX_DOM_CANDIDATES
                    ):
                        raise SyntheticDomFixtureError("DOM_CANDIDATE_LIMIT_EXCEEDED")
                    if batch_count + len(tranches) > MAX_DOM_BATCHES:
                        raise SyntheticDomFixtureError("DOM_BATCH_LIMIT_EXCEEDED")
                    if detail_count + len(cycle_targets) > MAX_DOM_DETAILS:
                        raise SyntheticDomFixtureError("DOM_DETAIL_LIMIT_EXCEEDED")

                    detail_fixtures = cycle_fixture.details
                    if tuple(item.target for item in detail_fixtures) != cycle_targets:
                        raise SyntheticDomFixtureError("DOM_DETAIL_PLAN_INVALID")
                    details: dict[str, FakeJobPostDetailV1] = {}
                    feed_authors = {
                        card.canonical_post_url: card.author_label
                        for tranche in tranches
                        for card in tranche.cards
                    }
                    for detail_fixture in detail_fixtures:
                        _require_time(deadline)
                        page.set_content(
                            detail_fixture.html,
                            wait_until="domcontentloaded",
                            timeout=_remaining_timeout_ms(deadline),
                        )
                        snapshot = snapshot_exact_post_detail(
                            page,
                            requested_url=detail_fixture.target,
                        )
                        if feed_authors.get(detail_fixture.target) != snapshot.author_label:
                            raise SyntheticDomFixtureError("DOM_AUTHOR_MISMATCH")
                        details[detail_fixture.target] = _to_fake_detail(
                            snapshot,
                            target=detail_fixture.target,
                            account_uuid=account_uuid,
                        )
                        if snapshot.body is not None:
                            visible_texts.append(snapshot.body)

                    batches = tuple(
                        FakeJobFeedBatchV1(
                            batch_index=tranche.batch_index,
                            account_uuid=account_uuid,
                            query=manifest.protocol_query,
                            page_url=SYNTHETIC_SEARCH_URL,
                            state=FakePageState.READY,
                            cards=cycle_cards[tranche.batch_index],
                            has_more=tranche.batch_index + 1 < len(tranches),
                        )
                        for tranche in tranches
                    )
                    session = _DomFixtureSession(
                        batches=batches,
                        details=details,
                        account_uuid=account_uuid,
                        query=manifest.protocol_query,
                    )
                    cycles.append(
                        SyntheticCycleInput(session=session, selected_targets=cycle_targets)
                    )
                    candidate_count += sum(len(item.cards) for item in tranches)
                    batch_count += len(tranches)
                    target_count += len(cycle_targets)
                    duplicate_count += cycle_duplicates
                    detail_count += len(details)
            finally:
                browser.close()
    except SyntheticDomFixtureError:
        raise
    except UiCardSnapshotError as error:
        raise SyntheticDomFixtureError(error.code) from None
    except PlaywrightError, OSError, RuntimeError, ValueError:
        raise SyntheticDomFixtureError("DOM_RENDER_FAILED") from None

    return DomFixtureCapture(
        protocol_query=manifest.protocol_query,
        cycles=tuple(cycles),
        batch_count=batch_count,
        candidate_count=candidate_count,
        target_count=target_count,
        duplicate_count=duplicate_count,
        detail_count=detail_count,
        blocked_request_count=blocked_request_count,
        visible_texts=tuple(visible_texts),
    )


def _decode_manifest(value: object) -> _DomManifest:
    manifest = _exact_object(value, {"schema_version", "fixture_kind", "query", "cycles"})
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["fixture_kind"] != "SYNTHETIC_DOM_ONLY"
    ):
        raise SyntheticDomFixtureError("DOM_MANIFEST_INVALID")
    query = manifest["query"]
    if (
        not isinstance(query, str)
        or not query
        or len(query) > 120
        or any(ord(character) < 32 for character in query)
    ):
        raise SyntheticDomFixtureError("DOM_QUERY_INVALID")
    raw_cycles = manifest["cycles"]
    if type(raw_cycles) is not list:
        raise SyntheticDomFixtureError("DOM_CYCLE_COUNT_INVALID")
    raw_cycles = cast(list[object], raw_cycles)
    if not 1 <= len(raw_cycles) <= MAX_DOM_CYCLES:
        raise SyntheticDomFixtureError("DOM_CYCLE_COUNT_INVALID")
    cycles: list[_DomCycleFixture] = []
    for raw_cycle in raw_cycles:
        cycle = _exact_object(raw_cycle, {"feed_html", "detail_pages"})
        feed_html = _bounded_html(cycle["feed_html"])
        raw_details = cycle["detail_pages"]
        if type(raw_details) is not list:
            raise SyntheticDomFixtureError("DOM_DETAIL_PLAN_INVALID")
        raw_details = cast(list[object], raw_details)
        if not 1 <= len(raw_details) <= MAX_DOM_DETAILS:
            raise SyntheticDomFixtureError("DOM_DETAIL_PLAN_INVALID")
        details: list[_DomDetailFixture] = []
        targets: set[str] = set()
        for raw_detail in raw_details:
            detail = _exact_object(raw_detail, {"target", "html"})
            target_value = detail["target"]
            if not isinstance(target_value, str):
                raise SyntheticDomFixtureError("DOM_DETAIL_TARGET_INVALID")
            if not is_reserved_synthetic_post_url(target_value) or target_value in targets:
                raise SyntheticDomFixtureError("DOM_DETAIL_TARGET_INVALID")
            targets.add(target_value)
            details.append(
                _DomDetailFixture(target=target_value, html=_bounded_html(detail["html"]))
            )
        cycles.append(_DomCycleFixture(feed_html=feed_html, details=tuple(details)))
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except UnicodeError, TypeError, ValueError, RecursionError:
        raise SyntheticDomFixtureError("DOM_MANIFEST_INVALID") from None
    if len(canonical) > MAX_DOM_MANIFEST_BYTES:
        raise SyntheticDomFixtureError("DOM_MANIFEST_TOO_LARGE")
    plan_digest = hashlib.sha256(canonical).hexdigest()
    return _DomManifest(
        query=query,
        cycles=tuple(cycles),
        protocol_query=f"dom-fixture-{plan_digest[:32]}",
    )


def _exact_object(value: object, expected_keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise SyntheticDomFixtureError("DOM_MANIFEST_INVALID")
    result = cast(dict[str, object], value)
    if any(type(key) is not str for key in result) or set(result) != expected_keys:
        raise SyntheticDomFixtureError("DOM_MANIFEST_INVALID")
    return result


def _bounded_html(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SyntheticDomFixtureError("DOM_HTML_INVALID")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise SyntheticDomFixtureError("DOM_HTML_INVALID") from None
    if len(encoded) > MAX_DOM_MANIFEST_BYTES:
        raise SyntheticDomFixtureError("DOM_MANIFEST_TOO_LARGE")
    return value


def _feed_cards(
    tranches: Sequence[FeedTrancheSnapshot],
    *,
    account_uuid: UUID,
) -> tuple[list[tuple[FakeJobCardV1, ...]], tuple[str, ...], int]:
    batches: list[tuple[FakeJobCardV1, ...]] = []
    seen_authors: dict[str, str] = {}
    targets: list[str] = []
    duplicate_count = 0
    for tranche in tranches:
        cards: list[FakeJobCardV1] = []
        for card in tranche.cards:
            previous_author = seen_authors.get(card.canonical_post_url)
            if previous_author is None:
                targets.append(card.canonical_post_url)
                seen_authors[card.canonical_post_url] = card.author_label
            else:
                duplicate_count += 1
                if previous_author != card.author_label:
                    raise SyntheticDomFixtureError("DOM_AUTHOR_MISMATCH")
            cards.append(
                FakeJobCardV1(
                    account_uuid=account_uuid,
                    local_card_id=card.local_card_id,
                    canonical_post_url=card.canonical_post_url,
                    author_marker=card.author_label.removeprefix("@"),
                )
            )
        batches.append(tuple(cards))
    if not targets:
        raise SyntheticDomFixtureError("DOM_FEED_EMPTY")
    return batches, tuple(targets), duplicate_count


def _to_fake_detail(
    snapshot: PostDetailSnapshot,
    *,
    target: str,
    account_uuid: UUID,
) -> FakeJobPostDetailV1:
    return FakeJobPostDetailV1(
        requested_url=target,
        page_url=target,
        canonical_post_url=snapshot.canonical_post_url,
        account_uuid=account_uuid,
        state=FakePageState.READY,
        author_marker=snapshot.author_label.removeprefix("@"),
        body=snapshot.body,
        body_visibility=snapshot.body_visibility,
        post_kind=snapshot.post_kind,
        body_truncated=snapshot.body_truncated,
        collapsed_content=snapshot.collapsed_content,
    )


def _require_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise SyntheticDomFixtureError("DOM_CAPTURE_TIMEOUT")


def _remaining_timeout_ms(deadline: float) -> int:
    _require_time(deadline)
    remaining = int((deadline - time.monotonic()) * 1_000)
    if remaining < 1:
        raise SyntheticDomFixtureError("DOM_CAPTURE_TIMEOUT")
    return min(remaining, MAX_BROWSER_OPERATION_MS)
