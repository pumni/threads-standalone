"""Offline Chromium source for the B1 browser-watch functional slice.

This adapter serves only a private synthetic HTML manifest on the reserved
``threads.example.invalid`` origin. Every other browser request is aborted.
Its output is mapped into the existing BJ-02 fake session contract so BJ-01,
BJ-03, and BJ-04 retain their established behavior and provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from playwright.sync_api import Browser, BrowserContext, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from threads_platform.browser.contracts import BrowserAdapterError, BrowserRuntimeUnavailable
from threads_platform.browser.job_observation import (
    SYNTHETIC_ORIGIN,
    SYNTHETIC_SEARCH_URL,
    FakeBodyVisibility,
    FakeJobCardV1,
    FakeJobFeedBatchV1,
    FakeJobPostDetailV1,
    FakePageState,
    ObservedPostKind,
)
from threads_platform.browser.threads_job_ui_reader import (
    ThreadsJobUiReader,
    UiBodyVisibility,
    UiFeedBatch,
    UiPageState,
    UiPostKind,
    UiPostTarget,
)
from threads_platform.standalone.browser_job_watch import SyntheticCycleInput

_MAX_FIXTURE_CYCLES = 5
_MAX_POSTS_PER_CYCLE = 20
_MAX_FIXTURE_HTML_CODEPOINTS = 1_100_000
_MAX_BROWSER_SECONDS = 30.0
_QUERY_MAX_CODEPOINTS = 120
_ACCOUNT_ALIAS_RE = re.compile(r"^(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})$")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?84|0)(?:[ \t().-]*\d){8,10}(?!\w)")
_REMOTE_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+")


@dataclass(frozen=True, slots=True, repr=False)
class UiFixtureDetailPage:
    target: str = field(repr=False)
    html: str = field(repr=False)

    def __repr__(self) -> str:
        return "UiFixtureDetailPage(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class UiFixtureCycle:
    feed_html: str = field(repr=False)
    detail_pages: tuple[UiFixtureDetailPage, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"UiFixtureCycle(detail_count={len(self.detail_pages)})"


@dataclass(frozen=True, slots=True, repr=False)
class UiFixtureManifest:
    query: str = field(repr=False)
    checkpoint_query: str = field(repr=False)
    cycles: tuple[UiFixtureCycle, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"UiFixtureManifest(cycle_count={len(self.cycles)})"


def decode_ui_fixture_manifest(
    value: object,
    *,
    max_cycles: int,
    max_posts_per_cycle: int,
) -> UiFixtureManifest:
    """Validate the exact local-browser fixture schema and operation bounds."""

    document = _exact_object(value, {"schema_version", "fixture_kind", "query", "cycles"})
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["fixture_kind"] != "SYNTHETIC_BROWSER_UI"
    ):
        raise BrowserAdapterError("UI_FIXTURE_INVALID")
    query = document["query"]
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > _QUERY_MAX_CODEPOINTS
        or _contains_contact_or_remote_url(query)
    ):
        raise BrowserAdapterError("UI_FIXTURE_QUERY_INVALID")
    if type(max_cycles) is not int or not 1 <= max_cycles <= _MAX_FIXTURE_CYCLES or max_cycles != 1:
        raise BrowserAdapterError("UI_FIXTURE_SINGLE_CYCLE_ONLY")
    if type(max_posts_per_cycle) is not int or not 1 <= max_posts_per_cycle <= _MAX_POSTS_PER_CYCLE:
        raise BrowserAdapterError("UI_FIXTURE_POST_LIMIT_INVALID")
    raw_cycles = document["cycles"]
    if type(raw_cycles) is not list:
        raise BrowserAdapterError("UI_FIXTURE_SINGLE_CYCLE_ONLY")
    cycle_values = cast(list[object], raw_cycles)
    if len(cycle_values) != 1:
        raise BrowserAdapterError("UI_FIXTURE_SINGLE_CYCLE_ONLY")
    cycle = _exact_object(cycle_values[0], {"feed_html", "detail_pages"})
    feed_html = _html(cycle["feed_html"])
    raw_details = cycle["detail_pages"]
    if type(raw_details) is not list:
        raise BrowserAdapterError("UI_FIXTURE_DETAIL_LIMIT_INVALID")
    detail_values = cast(list[object], raw_details)
    if not 1 <= len(detail_values) <= max_posts_per_cycle:
        raise BrowserAdapterError("UI_FIXTURE_DETAIL_LIMIT_INVALID")
    details: list[UiFixtureDetailPage] = []
    seen_targets: set[str] = set()
    for raw_detail in detail_values:
        detail = _exact_object(raw_detail, {"target", "html"})
        target = detail["target"]
        if not isinstance(target, str) or not _is_reserved_post_url(target):
            raise BrowserAdapterError("UI_FIXTURE_TARGET_INVALID")
        if target in seen_targets:
            raise BrowserAdapterError("UI_FIXTURE_TARGET_DUPLICATE")
        seen_targets.add(target)
        details.append(UiFixtureDetailPage(target, _html(detail["html"])))
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    digest = hashlib.sha256(canonical).hexdigest()
    return UiFixtureManifest(
        query,
        f"ui-fixture-{digest}",
        (UiFixtureCycle(feed_html, tuple(details)),),
    )


class BrowserUiFixtureSession:
    """Playwright session implementing BJ-02's seam using the new B1 reader."""

    def __init__(
        self,
        cycle: UiFixtureCycle,
        *,
        account_uuid: UUID,
        query: str,
        search_query: str,
        account_alias: str,
    ) -> None:
        if not _ACCOUNT_ALIAS_RE.fullmatch(account_alias):
            raise BrowserAdapterError("ACCOUNT_ALIAS_INVALID")
        self._account_uuid = account_uuid
        self._query = query
        self._search_query = search_query
        self._started_at = time.monotonic()
        self._expected_actor = account_alias.split("-", maxsplit=1)[1]
        self._feed_html = cycle.feed_html
        self._details_by_target = {item.target: item.html for item in cycle.detail_pages}
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._route_blocked_count = 0
        self._reader: ThreadsJobUiReader | None = None
        self._batches: tuple[UiFeedBatch, ...] = ()
        self._cards_by_target: dict[str, UiPostTarget] = {}
        self._detail_count = 0
        self._open()

    def __repr__(self) -> str:
        return (
            "BrowserUiFixtureSession("
            f"batch_count={len(self._batches)}, target_count={len(self._cards_by_target)})"
        )

    @property
    def batch_count(self) -> int:
        return len(self._batches)

    @property
    def candidate_count(self) -> int:
        return sum(len(batch.cards) for batch in self._batches)

    @property
    def target_count(self) -> int:
        return len(self._cards_by_target)

    @property
    def duplicate_count(self) -> int:
        return max(0, self.candidate_count - self.target_count)

    @property
    def detail_count(self) -> int:
        return self._detail_count

    @property
    def scroll_count(self) -> int:
        return 0 if self._reader is None else self._reader.scroll_count

    @property
    def blocked_request_count(self) -> int:
        reader_count = 0 if self._reader is None else self._reader.blocked_request_count
        return self._route_blocked_count + reader_count

    @property
    def selected_targets(self) -> tuple[str, ...]:
        return tuple(self._cards_by_target)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
            self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except PlaywrightError:
                pass
            self._playwright = None

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        if account_uuid != self._account_uuid or query != self._query or max_cards < 1:
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        return self._batch(0, max_cards=max_cards)

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
            or increment != 1
            or not 0 <= next_batch_index < len(self._batches)
            or max_cards < 1
        ):
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        return self._batch(next_batch_index, max_cards=max_cards)

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> FakeJobPostDetailV1 | None:
        if account_uuid != self._account_uuid:
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        card = self._cards_by_target.get(canonical_post_url)
        if card is None:
            return None
        if canonical_post_url not in self._details_by_target:
            return None
        if self._reader is None:
            raise BrowserAdapterError("BROWSER_SESSION_MISMATCH")
        self._check_time_budget()
        self._detail_count += 1
        detail = self._reader.read_exact_post(
            canonical_post_url,
            expected_actor=self._expected_actor,
        )
        self._check_time_budget()
        if detail.body is not None and _contains_contact_or_remote_url(detail.body):
            raise BrowserAdapterError("UI_FIXTURE_BODY_INVALID")
        return FakeJobPostDetailV1(
            requested_url=detail.requested_url,
            page_url=detail.page_url,
            canonical_post_url=detail.canonical_post_url,
            account_uuid=self._account_uuid,
            state=_fake_page_state(detail.state),
            author_marker=detail.author_username,
            body=detail.body,
            body_visibility=_fake_body_visibility(detail.body_visibility),
            post_kind=_fake_post_kind(detail.post_kind),
            body_truncated=detail.body_truncated,
            image_only=detail.image_only,
        )

    def _open(self) -> None:
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                viewport={"width": 1_000, "height": 700},
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
            )
            self._context.route("**/*", self._route_request)
            self._page = self._context.new_page()
            self._reader = ThreadsJobUiReader(
                self._page,
                origin=SYNTHETIC_ORIGIN,
            )
            self._batches = self._reader.search(
                self._search_query,
                expected_actor=self._expected_actor,
            )
            self._check_time_budget()
            if self._batches and self._batches[0].state is not UiPageState.READY:
                state_code = {
                    UiPageState.REDIRECTED: "BROWSER_REDIRECT_DETECTED",
                    UiPageState.CHALLENGE: "CHALLENGE_REQUIRED",
                    UiPageState.LOGIN_WALL: "SESSION_EXPIRED",
                    UiPageState.RATE_LIMITED: "RATE_LIMITED",
                    UiPageState.UNKNOWN: "UNSUPPORTED_UI_STATE",
                    UiPageState.READY: "BROWSER_READ_FAILED",
                }[self._batches[0].state]
                raise BrowserAdapterError(state_code)
            for batch in self._batches:
                for card in batch.cards:
                    if not _is_synthetic_actor(card.author_username):
                        raise BrowserAdapterError("UI_FIXTURE_AUTHOR_INVALID")
                    previous = self._cards_by_target.get(card.canonical_post_url)
                    if previous is not None and previous.author_username != card.author_username:
                        raise BrowserAdapterError("BROWSER_AUTHOR_MISMATCH")
                    self._cards_by_target.setdefault(card.canonical_post_url, card)
            if len(self._cards_by_target) > _MAX_POSTS_PER_CYCLE:
                raise BrowserAdapterError("BROWSER_DETAIL_LIMIT_EXCEEDED")
        except BrowserAdapterError:
            self.close()
            raise
        except KeyboardInterrupt:
            self.close()
            raise
        except PlaywrightError as error:
            self.close()
            message = str(error).casefold()
            if "executable doesn't exist" in message or "browser has been closed" in message:
                raise BrowserRuntimeUnavailable("BROWSER_RUNTIME_UNAVAILABLE") from None
            raise BrowserRuntimeUnavailable() from None
        except Exception:  # noqa: BLE001 - browser startup details stay redacted at this boundary.
            self.close()
            raise BrowserAdapterError("BROWSER_RUNTIME_UNAVAILABLE") from None

    def _route_request(self, route: Route) -> None:
        request = route.request
        try:
            parsed = urlsplit(request.url)
        except ValueError:
            self._block(route)
            return
        if parsed.scheme != "https" or parsed.netloc != "threads.example.invalid":
            if request.is_navigation_request() and self._reader is not None:
                self._reader.note_blocked_off_origin_navigation()
            self._block(route)
            return
        if request.method != "GET":
            self._block(route)
            return
        if parsed.path == "/search":
            try:
                query = parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                self._block(route)
                return
            if query == {"q": [self._search_query]}:
                route.fulfill(
                    status=200, body=self._feed_html, content_type="text/html; charset=utf-8"
                )
                return
        target = f"https://threads.example.invalid{parsed.path}"
        detail_html = self._details_by_target.get(target)
        if detail_html is not None and not parsed.query and not parsed.fragment:
            route.fulfill(status=200, body=detail_html, content_type="text/html; charset=utf-8")
            return
        self._block(route)

    def _check_time_budget(self) -> None:
        if time.monotonic() - self._started_at >= _MAX_BROWSER_SECONDS:
            raise BrowserAdapterError("BROWSER_READ_TIMEOUT")

    def _block(self, route: Route) -> None:
        self._route_blocked_count += 1
        try:
            route.abort("blockedbyclient")
        except PlaywrightError:
            pass

    def _batch(self, index: int, *, max_cards: int) -> FakeJobFeedBatchV1:
        batch = self._batches[index]
        if len(batch.cards) > max_cards:
            raise BrowserAdapterError("BROWSER_CARD_LIMIT_EXCEEDED")
        cards = tuple(
            FakeJobCardV1(
                account_uuid=self._account_uuid,
                local_card_id=f"ui-{index:02d}-{card_index:03d}",
                canonical_post_url=card.canonical_post_url,
                author_marker=card.author_username,
            )
            for card_index, card in enumerate(batch.cards, start=1)
        )
        page_url = batch.page_url
        return FakeJobFeedBatchV1(
            batch_index=index,
            account_uuid=self._account_uuid,
            query=self._query,
            page_url=page_url if batch.state is not UiPageState.READY else SYNTHETIC_SEARCH_URL,
            state=_fake_page_state(batch.state),
            cards=cards,
            has_more=batch.has_more,
        )


def make_ui_fixture_cycle(
    manifest: UiFixtureManifest,
    *,
    account_uuid: UUID,
    account_alias: str,
) -> tuple[BrowserUiFixtureSession, SyntheticCycleInput]:
    """Open Chromium and construct the cached BJ-02 cycle for the watch engine."""

    session = BrowserUiFixtureSession(
        manifest.cycles[0],
        account_uuid=account_uuid,
        query=manifest.checkpoint_query,
        search_query=manifest.query,
        account_alias=account_alias,
    )
    selected_targets = session.selected_targets
    if not selected_targets:
        session.close()
        raise BrowserAdapterError("BROWSER_EMPTY_FEED")
    return session, SyntheticCycleInput(session=session, selected_targets=selected_targets)


def _fake_page_state(value: UiPageState) -> FakePageState:
    return {
        UiPageState.READY: FakePageState.READY,
        UiPageState.REDIRECTED: FakePageState.REDIRECTED,
        UiPageState.CHALLENGE: FakePageState.CHALLENGE,
        UiPageState.LOGIN_WALL: FakePageState.LOGIN_WALL,
        UiPageState.RATE_LIMITED: FakePageState.RATE_LIMITED,
        UiPageState.UNKNOWN: FakePageState.UNKNOWN,
    }[value]


def _fake_body_visibility(value: UiBodyVisibility) -> FakeBodyVisibility:
    return {
        UiBodyVisibility.FULL_VISIBLE: FakeBodyVisibility.FULL_VISIBLE,
        UiBodyVisibility.PARTIAL: FakeBodyVisibility.PARTIAL,
        UiBodyVisibility.UNKNOWN: FakeBodyVisibility.UNKNOWN,
    }[value]


def _fake_post_kind(value: UiPostKind) -> ObservedPostKind:
    return {
        UiPostKind.ORIGINAL: ObservedPostKind.ORIGINAL,
        UiPostKind.REPOST: ObservedPostKind.REPOST,
        UiPostKind.QUOTE: ObservedPostKind.QUOTE,
        UiPostKind.REPLY: ObservedPostKind.REPLY,
        UiPostKind.UNKNOWN: ObservedPostKind.UNKNOWN,
    }[value]


def _html(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_FIXTURE_HTML_CODEPOINTS
        or "\x00" in value
    ):
        raise BrowserAdapterError("UI_FIXTURE_HTML_INVALID")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise BrowserAdapterError("UI_FIXTURE_HTML_INVALID") from None
    return value


def _contains_contact_or_remote_url(value: str) -> bool:
    return bool(_EMAIL_RE.search(value) or _PHONE_RE.search(value) or _REMOTE_URL_RE.search(value))


def _is_reserved_post_url(value: str) -> bool:
    if len(value) > 2_048 or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    valid_post_path = re.fullmatch(
        r"/@(?P<username>[A-Za-z0-9._:-]{1,96})/"
        r"(?:post-[A-Za-z0-9._:-]{1,128}|post/[A-Za-z0-9._:-]{1,128})",
        parsed.path,
    )
    return (
        parsed.scheme == "https"
        and parsed.netloc == "threads.example.invalid"
        and parsed.hostname == "threads.example.invalid"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and valid_post_path is not None
        and _is_synthetic_actor(valid_post_path.group("username"))
        and not parsed.query
        and not parsed.fragment
    )


def _is_synthetic_actor(value: str) -> bool:
    return value.startswith(("synthetic-", "test-"))


def _exact_object(value: object, expected_keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise BrowserAdapterError("UI_FIXTURE_INVALID")
    raw = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != expected_keys:
        raise BrowserAdapterError("UI_FIXTURE_INVALID")
    return cast(dict[str, object], value)
