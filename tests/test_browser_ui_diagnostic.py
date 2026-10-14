from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import pytest

from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserProcessCrashed,
    BrowserRuntimeUnavailable,
    RemoteSessionStateUncertain,
)
from threads_platform.browser.playwright_engine import (
    _OPERATOR_CLICK_OBSERVER_SCRIPT,
    PlaywrightBrowserEngine,
)
from threads_platform.browser.ui_diagnostic import (
    DIAGNOSTIC_SCRIPT,
    UiDiagnosticSchemaError,
    UiDiagnosticSnapshot,
    validate_ui_diagnostic_snapshot,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.browser_nurture_state import (
    BrowserNurtureCheckpointV2,
    BrowserNurtureConfigV1,
    BrowserNurtureStateStore,
    browser_nurture_policy_fingerprint,
    validate_browser_nurture_config,
)
from threads_platform.standalone.browser_ui_diagnostic import (
    BrowserUiDiagnosticSession,
    _arm_operator_action_capture,
    run_browser_ui_diagnostic,
    wait_for_stable_ui_snapshot,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

_SYNTHETIC_ORIGIN = "https://threads.example.invalid"
_PRIVATE_USERNAME = "private_owner_71"
_PRIVATE_QUERY = "private configured query 26"
_PRIVATE_POST_TEXT = "private post body 913"
_PRIVATE_POST_ID = "privatepostid71"
_PRIVATE_CANDIDATE = "private_candidate_86"


def _valid_raw_snapshot() -> dict[str, object]:
    return {
        "status": "OBSERVED",
        "route_class": "SEARCH",
        "counts": {
            "navigation_landmarks": "ONE",
            "main_landmarks": "ONE",
            "header_landmarks": "ZERO",
            "article_landmarks": "ONE",
            "search_controls": "ONE",
            "profile_controls": "ONE",
            "home_controls": "ZERO",
            "searchboxes": "ONE",
            "self_profile_links": "ONE",
            "other_profile_links": "MANY",
            "post_permalinks": "ONE",
            "author_profile_links": "MANY",
            "panels": "ONE",
            "visible_candidates": "MANY",
            "offscreen_candidates": "ZERO",
            "clipped_candidates": "ZERO",
        },
        "nodes": [
            {"id": 0, "role": "NAVIGATION", "tag": "NAV", "label": "OTHER"},
            {"id": 1, "role": "BUTTON", "tag": "BUTTON", "label": "SEARCH"},
        ],
        "relations": [
            {
                "source": 0,
                "target": 1,
                "relation": "CONTROL_DESCENDANT_OF_NAV",
            }
        ],
        "operator_action": None,
        "operator_action_status": "UNVERIFIED",
        "search_input_matches_query": True,
        "url_query_matches": False,
        "query_application_status": "UNVERIFIED",
        "search_results_status": "UNVERIFIED",
        "self_profile_match": True,
        "body_status": "UNKNOWN",
        "document_ready": True,
    }


def _config(account_id: UUID) -> BrowserNurtureConfigV1:
    return validate_browser_nurture_config(
        {
            "schema_version": 1,
            "account_uuid": str(account_id),
            "expected_username": _PRIVATE_USERNAME,
            "topics": [_PRIVATE_QUERY],
            "relevance_terms": [_PRIVATE_QUERY],
            "max_posts_per_cycle": 10,
            "idle_seconds": 60,
        }
    )


def _report_stage(report: dict[str, object], stage: str) -> dict[str, object]:
    stages = cast(dict[str, dict[str, object]], report["stages"])
    return stages[stage]


def test_diagnostic_schema_rejects_page_strings_and_unbounded_values() -> None:
    private_value = "cookie=private-cookie; username=private-name; https://private.invalid/post/91"
    raw = _valid_raw_snapshot()
    raw["content"] = private_value
    with pytest.raises(UiDiagnosticSchemaError):
        validate_ui_diagnostic_snapshot(raw)

    raw = _valid_raw_snapshot()
    raw["operator_action"] = {
        "source_route": "HOME",
        "role": "BUTTON",
        "tag": "DIV",
        "label": "OTHER",
        "within_unique_navigation": True,
        "stable_semantic_locator_unique": False,
        "before_searchboxes": "ZERO",
        "is_post_permalink": False,
        "destination_route": "SEARCH",
        "destination_searchboxes": "ONE",
        "search_input_matches_query": True,
        "url_query_matches": False,
        "search_state_change_status": "CHANGE_VERIFIED",
        "search_destination_relation": False,
        "clicked_post_matches_destination": False,
        "unique_co_container_author_matches_clicked_post": False,
        "target_path": "/@private_candidate_86/post/privatepostid71",
    }
    with pytest.raises(UiDiagnosticSchemaError):
        validate_ui_diagnostic_snapshot(raw)

    raw = _valid_raw_snapshot()
    nodes = cast(list[dict[str, object]], raw["nodes"])
    nodes[1]["label"] = private_value
    with pytest.raises(UiDiagnosticSchemaError):
        validate_ui_diagnostic_snapshot(raw)

    raw = _valid_raw_snapshot()
    relations = cast(list[dict[str, object]], raw["relations"])
    relations[0]["relation"] = private_value
    with pytest.raises(UiDiagnosticSchemaError):
        validate_ui_diagnostic_snapshot(raw)

    raw = _valid_raw_snapshot()
    raw["document_ready"] = "ready=true"
    with pytest.raises(UiDiagnosticSchemaError):
        validate_ui_diagnostic_snapshot(raw)

    for field, value in (
        ("query_application_status", "APPLIED"),
        ("search_results_status", "LOADED"),
        ("body_status", "PRESENT"),
    ):
        raw = _valid_raw_snapshot()
        raw[field] = value
        with pytest.raises(UiDiagnosticSchemaError):
            validate_ui_diagnostic_snapshot(raw)


def test_diagnostic_report_contains_only_allowlisted_semantics() -> None:
    snapshot = validate_ui_diagnostic_snapshot(_valid_raw_snapshot())
    encoded = json.dumps(snapshot.to_document(), sort_keys=True)

    assert '"route_class": "SEARCH"' in encoded
    assert "CONTROL_DESCENDANT_OF_NAV" in encoded
    for private_value in (
        _PRIVATE_USERNAME,
        _PRIVATE_QUERY,
        _PRIVATE_POST_TEXT,
        _PRIVATE_POST_ID,
        _PRIVATE_CANDIDATE,
        "sessionid=private-cookie",
        "https://private.invalid",
    ):
        assert private_value not in encoded


def test_browser_probe_script_is_read_only_and_has_no_credential_or_upload_access() -> None:
    script = (DIAGNOSTIC_SCRIPT + _OPERATOR_CLICK_OBSERVER_SCRIPT).lower()
    for forbidden in (
        "document.cookie",
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "fetch(",
        "xmlhttprequest",
        "websocket",
        "sendbeacon",
        ".click(",
        "dispatchevent(",
        "form.submit(",
        "innerhtml",
        "screenshot",
        "innertext",
        "textcontent",
    ):
        assert forbidden not in script


class _SyntheticUiDiagnosticSession(Protocol):
    async def install_synthetic_fixture_pages(
        self,
        pages: Mapping[str, str],
        *,
        origin: str,
    ) -> None: ...

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None: ...

    async def arm_ui_diagnostic_operator_click(self, *, source_route: str) -> None: ...

    async def verify_ui_account(self, expected_actor: str, *, origin: str) -> object: ...

    async def search_ui_feed(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> object: ...

    async def inspect_ui_diagnostic(
        self,
        *,
        expected_username: str,
        configured_topic: str,
    ) -> UiDiagnosticSnapshot: ...

    async def close(self) -> None: ...


def test_synthetic_chromium_home_search_detail_report_is_private(tmp_path: Path) -> None:
    home_url = f"{_SYNTHETIC_ORIGIN}/"
    search_url = f"{_SYNTHETIC_ORIGIN}/search"
    query_url = f"{_SYNTHETIC_ORIGIN}/?q={quote(_PRIVATE_QUERY, safe='')}"
    detail_url = f"{_SYNTHETIC_ORIGIN}/@{_PRIVATE_CANDIDATE}/post/{_PRIVATE_POST_ID}"
    unknown_detail_url = f"{_SYNTHETIC_ORIGIN}/thread/{_PRIVATE_POST_ID}"
    visibility_url = f"{_SYNTHETIC_ORIGIN}/visibility"
    home_html = f"""
      <nav role="navigation" aria-label="Primary">
        <a href="/" aria-label="Home">Home</a>
        <button aria-label="Search">Search</button>
        <a href="/@{_PRIVATE_USERNAME.upper()}" aria-label="Profile">Profile</a>
      </nav>
      <div role="dialog"><form><input role="searchbox" aria-label="Search"
        value="{_PRIVATE_QUERY}"></form></div>
      <main><article><header>
        <a href="/@{_PRIVATE_CANDIDATE}">@{_PRIVATE_CANDIDATE}</a>
        <a href="/@{_PRIVATE_CANDIDATE}/post/{_PRIVATE_POST_ID}">time</a>
      </header><p>{_PRIVATE_POST_TEXT}</p></article></main>
    """
    search_html = f"""
      <nav role="navigation"><button aria-label="Search">Search</button></nav>
      <div role="dialog"><form><input role="searchbox" aria-label="Search"
        value="{_PRIVATE_QUERY}"></form>
        <article><header>
          <a href="/@{_PRIVATE_CANDIDATE}">@{_PRIVATE_CANDIDATE}</a>
          <a href="/@{_PRIVATE_CANDIDATE}/post/{_PRIVATE_POST_ID}">time</a>
        </header><p>{_PRIVATE_POST_TEXT}</p></article>
      </div>
    """
    detail_html = f"""
      <main><article><header>
        <a href="/@{_PRIVATE_CANDIDATE}">@{_PRIVATE_CANDIDATE}</a>
        <a href="/@{_PRIVATE_CANDIDATE}/post/{_PRIVATE_POST_ID}">time</a>
      </header><p>{_PRIVATE_POST_TEXT}</p><button aria-label="See more">See more</button>
      </article></main>
    """
    query_url_html = search_html.replace(f'value="{_PRIVATE_QUERY}"', 'value=""')
    visibility_html = """
      <nav role="navigation"><button aria-label="Search">Search</button></nav>
      <div aria-hidden="true"><button aria-label="Search">Hidden ancestor</button></div>
      <div hidden><button aria-label="Search">Hidden attribute ancestor</button></div>
      <div style="opacity:0"><button aria-label="Search">Transparent ancestor</button></div>
      <button aria-label="Search" style="position:fixed;left:-5000px;top:8px">Offscreen</button>
      <div style="overflow:hidden;width:0;height:0">
        <button aria-label="Search">Clipped</button>
      </div>
    """
    pages = {
        home_url: home_html,
        search_url: search_html,
        query_url: query_url_html,
        detail_url: detail_html,
        unknown_detail_url: detail_html,
        visibility_url: visibility_html,
    }

    async def observe_pages() -> list[dict[str, object]]:
        profile = tmp_path / "synthetic-diagnostic-profile"
        profile.mkdir()
        request = BrowserLaunchRequest(
            profile_directory=profile,
            network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            headless=True,
        )
        engine = PlaywrightBrowserEngine()
        try:
            raw_session = await engine.open(request)
        except BrowserRuntimeUnavailable as error:
            if error.code in {"BROWSER_BINARY_MISSING", "BROWSER_START_FAILED"}:
                pytest.skip("Playwright Chromium is not available in this environment")
            raise
        session = cast(_SyntheticUiDiagnosticSession, raw_session)
        reports: list[dict[str, object]] = []
        try:
            await session.install_synthetic_fixture_pages(pages, origin=_SYNTHETIC_ORIGIN)
            for url in (home_url, search_url, detail_url):
                await session.navigate(
                    url,
                    allowed_origins=frozenset({_SYNTHETIC_ORIGIN}),
                )
                snapshot = await session.inspect_ui_diagnostic(
                    expected_username=_PRIVATE_USERNAME,
                    configured_topic=_PRIVATE_QUERY,
                )
                reports.append(snapshot.to_document())
            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            home_overlay = await wait_for_stable_ui_snapshot(
                cast(BrowserUiDiagnosticSession, session),
                stage="search",
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            reports.append(home_overlay.to_document())
            await session.navigate(
                unknown_detail_url,
                allowed_origins=frozenset({_SYNTHETIC_ORIGIN}),
            )
            unknown_detail = await wait_for_stable_ui_snapshot(
                cast(BrowserUiDiagnosticSession, session),
                stage="detail",
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            reports.append(unknown_detail.to_document())
            await session.navigate(query_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            url_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            reports.append(url_snapshot.to_document())
            await session.navigate(visibility_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            visibility_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            reports.append(visibility_snapshot.to_document())
        finally:
            await session.close()
        return reports

    reports = asyncio.run(observe_pages())

    home, search, detail, home_overlay, unknown_detail, query_from_url, visibility = reports
    assert home["route_class"] == "HOME"
    assert home["self_profile_match"] is True
    assert search["route_class"] == "SEARCH"
    assert search["search_input_matches_query"] is True
    assert search["url_query_matches"] is False
    assert search["query_application_status"] == "UNVERIFIED"
    assert search["search_results_status"] == "UNVERIFIED"
    assert query_from_url["search_input_matches_query"] is False
    assert query_from_url["url_query_matches"] is True
    assert query_from_url["query_application_status"] == "UNVERIFIED"
    assert query_from_url["search_results_status"] == "UNVERIFIED"
    search_counts = cast(dict[str, object], search["counts"])
    search_relations = cast(list[dict[str, object]], search["relations"])
    detail_relations = cast(list[dict[str, object]], detail["relations"])
    assert search_counts["panels"] == "ONE"
    assert any(relation["relation"] == "PANEL_CONTAINS_SEARCHBOX" for relation in search_relations)
    assert any(
        relation["relation"] == "RESULT_POST_AND_AUTHOR_SHARE_CONTAINER"
        for relation in search_relations
    )
    assert detail["route_class"] == "POST"
    assert detail["body_status"] == "UNKNOWN"
    assert any(
        relation["relation"] == "DETAIL_POST_AND_AUTHOR_SHARE_CONTAINER"
        for relation in detail_relations
    )
    assert home_overlay["route_class"] == "HOME"
    assert home_overlay["search_input_matches_query"] is True
    assert home_overlay["url_query_matches"] is False
    assert cast(dict[str, object], home_overlay["counts"])["visible_candidates"] != "ZERO"
    assert unknown_detail["route_class"] == "OTHER"
    assert cast(dict[str, object], unknown_detail["counts"])["article_landmarks"] == "ONE"
    visibility_counts = cast(dict[str, object], visibility["counts"])
    assert visibility_counts["search_controls"] == "ONE"
    assert visibility_counts["offscreen_candidates"] == "ONE"
    assert visibility_counts["clipped_candidates"] == "ONE"
    encoded = json.dumps(reports, sort_keys=True)
    for private_value in (
        _PRIVATE_USERNAME,
        _PRIVATE_QUERY,
        _PRIVATE_POST_TEXT,
        _PRIVATE_POST_ID,
        _PRIVATE_CANDIDATE,
    ):
        assert private_value not in encoded


def test_synthetic_chromium_operator_click_correlation_is_bounded_and_private(
    tmp_path: Path,
) -> None:
    home_url = f"{_SYNTHETIC_ORIGIN}/"
    action_candidate = "phasebfixture"
    action_post_id = "phasebpost71"
    detail_path = f"/@{action_candidate}/post/{action_post_id}"
    detail_url = f"{_SYNTHETIC_ORIGIN}{detail_path}"
    home_html = f"""
      <style>[role="button"]{{display:inline-block;width:32px;height:32px}}</style>
      <div role="navigation">
        <div role="button" id="search-trigger" aria-label="Search"></div>
        <div role="button" id="search-overlay-trigger"></div>
        <div role="button" id="other-control"></div>
        <a href="/@{_PRIVATE_USERNAME}" aria-label="Profile">Profile</a>
      </div>
      <div><a href="/@{_PRIVATE_USERNAME}" aria-label="Profile">Profile</a></div>
      <script>
        document.querySelector('#search-trigger').addEventListener('click', () => {{
          window.location.href = '/search';
        }});
        document.querySelector('#search-overlay-trigger').addEventListener('click', () => {{
          document.body.insertAdjacentHTML('beforeend',
            '<input id="overlay-query" role="textbox" aria-label="Search" ' +
            'value="{_PRIVATE_QUERY}">');
        }});
      </script>
    """
    search_html = f"""
      <input id="query" role="textbox" aria-label="Search" value="{_PRIVATE_QUERY}">
      <a id="post-link" href="{detail_path}">time</a>
    """
    detail_html = f"""
      <div><a href="/@{action_candidate}">author</a>
      <a href="{detail_path}">time</a><p>{_PRIVATE_POST_TEXT}</p></div>
    """

    async def observe_transitions() -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        profile = tmp_path / "synthetic-action-profile"
        profile.mkdir()
        request = BrowserLaunchRequest(
            profile_directory=profile,
            network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            headless=True,
        )
        engine = PlaywrightBrowserEngine()
        try:
            browser_session = await engine.open(request)
        except BrowserRuntimeUnavailable as error:
            if error.code in {"BROWSER_BINARY_MISSING", "BROWSER_START_FAILED"}:
                pytest.skip("Playwright Chromium is not available in this environment")
            raise
        session = cast(_SyntheticUiDiagnosticSession, browser_session)
        try:
            await session.install_synthetic_fixture_pages(
                {
                    home_url: home_html,
                    f"{_SYNTHETIC_ORIGIN}/search": search_html,
                    detail_url: detail_html,
                },
                origin=_SYNTHETIC_ORIGIN,
            )
            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            home_state = await session.verify_ui_account(
                _PRIVATE_USERNAME,
                origin=_SYNTHETIC_ORIGIN,
            )
            page = cast(Any, session)._page
            await page.set_content(
                '<div role="navigation"><a href="/@wrongoperator" '
                'aria-label="Profile">Profile</a></div>'
                f'<a href="/@{_PRIVATE_USERNAME}" aria-label="Profile">Profile</a>'
            )
            with pytest.raises(BrowserAdapterError) as mismatch:
                await session.verify_ui_account(_PRIVATE_USERNAME, origin=_SYNTHETIC_ORIGIN)
            assert mismatch.value.code == "BROWSER_SESSION_MISMATCH"
            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            await session.arm_ui_diagnostic_operator_click(source_route="HOME")
            implementation = cast(Any, browser_session)
            binding_name = implementation._ui_diagnostic_click_binding_name
            binding_context_id = implementation._ui_diagnostic_click_context_id
            assert isinstance(binding_name, str)
            assert type(binding_context_id) is int
            assert await page.evaluate("(name) => typeof window[name]", binding_name) == "undefined"
            iframe_binding = await page.evaluate(
                """async (name) => {
                  const frame = document.createElement('iframe');
                  frame.srcdoc = '<!doctype html><body></body>';
                  document.body.append(frame);
                  await new Promise((resolve) => frame.addEventListener('load', resolve, {once: true}));
                  return typeof frame.contentWindow[name];
                }""",
                binding_name,
            )
            assert iframe_binding == "undefined"
            await page.evaluate(
                """() => document.querySelector('#other-control').dispatchEvent(
                  new MouseEvent('click', {bubbles: true})
                )"""
            )
            forged_payload = json.dumps(
                {
                    "event_is_trusted": True,
                    "source_route": "HOME",
                    "role": "BUTTON",
                    "tag": "DIV",
                    "label": "SEARCH",
                    "within_unique_navigation": True,
                    "stable_semantic_locator_unique": True,
                    "before_searchboxes": "ZERO",
                    "is_post_permalink": False,
                    "target_path": None,
                }
            )
            implementation._receive_ui_diagnostic_operator_click(
                {
                    "name": binding_name,
                    "executionContextId": binding_context_id + 10_000,
                    "payload": forged_payload,
                }
            )
            assert implementation._ui_diagnostic_operator_click is None
            forged_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            assert forged_snapshot.to_document()["operator_action_status"] == "UNVERIFIED"
            assert forged_snapshot.to_document()["operator_action"] is None
            await page.locator("#search-trigger").click()
            search_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            search_report = search_snapshot.to_document()

            await session.arm_ui_diagnostic_operator_click(source_route="SEARCH")
            await page.locator("#post-link").click()
            detail_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            detail_report = detail_snapshot.to_document()

            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            await page.evaluate(
                """() => {
                  const duplicate = document.createElement('div');
                  duplicate.setAttribute('role', 'button');
                  duplicate.setAttribute('aria-label', 'Search');
                  document.querySelector('[role="navigation"]').append(duplicate);
                }"""
            )
            await session.arm_ui_diagnostic_operator_click(source_route="HOME")
            await page.locator("#search-trigger").click()
            duplicate_search_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            duplicate_search_report = duplicate_search_snapshot.to_document()

            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            await session.arm_ui_diagnostic_operator_click(source_route="HOME")
            await page.locator("#search-overlay-trigger").click()
            overlay_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            overlay_report = overlay_snapshot.to_document()

            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            await page.evaluate(
                """() => {
                  const input = document.createElement('input');
                  input.setAttribute('role', 'textbox');
                  input.setAttribute('aria-label', 'Search');
                  document.body.append(input);
                }"""
            )
            await session.arm_ui_diagnostic_operator_click(source_route="HOME")
            await page.locator("#other-control").click()
            preexisting_search_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            preexisting_search_report = preexisting_search_snapshot.to_document()

            await session.navigate(home_url, allowed_origins=frozenset({_SYNTHETIC_ORIGIN}))
            await session.arm_ui_diagnostic_operator_click(source_route="HOME")
            await page.locator("#other-control").click()
            await page.evaluate(
                """() => {
                  const input = document.createElement('input');
                  input.setAttribute('role', 'textbox');
                  input.setAttribute('aria-label', 'Search');
                  document.body.append(input);
                }"""
            )
            manually_opened_search_snapshot = await session.inspect_ui_diagnostic(
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            manually_opened_search_report = manually_opened_search_snapshot.to_document()
        finally:
            await cast(Any, browser_session).close()
        return (
            {"state": str(home_state)},
            search_report,
            detail_report,
            duplicate_search_report,
            overlay_report,
            preexisting_search_report,
            manually_opened_search_report,
        )

    home, search, detail, duplicate_search, overlay, preexisting, manually_opened = asyncio.run(
        observe_transitions()
    )
    assert home["state"] == "READY"
    search_action = cast(dict[str, object], search["operator_action"])
    assert search["operator_action_status"] == "VERIFIED"
    assert search_action["source_route"] == "HOME"
    assert search_action["role"] == "BUTTON"
    assert search_action["tag"] == "DIV"
    assert search_action["label"] == "SEARCH"
    assert search_action["within_unique_navigation"] is True
    assert search_action["stable_semantic_locator_unique"] is True
    assert search_action["before_searchboxes"] == "ZERO"
    assert search_action["search_destination_relation"] is True
    assert search_action["destination_searchboxes"] == "ONE"
    assert search_action["search_input_matches_query"] is True
    assert search_action["url_query_matches"] is False
    assert search_action["search_state_change_status"] == "CHANGE_VERIFIED"
    detail_action = cast(dict[str, object], detail["operator_action"])
    assert detail_action["source_route"] == "SEARCH"
    assert detail_action["role"] == "LINK"
    assert detail_action["is_post_permalink"] is True
    assert detail_action["clicked_post_matches_destination"] is True
    assert detail_action["unique_co_container_author_matches_clicked_post"] is True
    assert detail["route_class"] == "POST"
    assert detail["body_status"] == "UNKNOWN"
    duplicate_action = cast(dict[str, object], duplicate_search["operator_action"])
    assert duplicate_action["source_route"] == "HOME"
    assert duplicate_action["label"] == "SEARCH"
    assert duplicate_action["stable_semantic_locator_unique"] is False
    assert duplicate_action["search_state_change_status"] == "CHANGE_VERIFIED"
    assert duplicate_action["search_destination_relation"] is False
    overlay_action = cast(dict[str, object], overlay["operator_action"])
    assert overlay["route_class"] == "HOME"
    assert overlay_action["destination_route"] == "HOME"
    assert overlay_action["stable_semantic_locator_unique"] is False
    assert overlay_action["before_searchboxes"] == "ZERO"
    assert overlay_action["search_state_change_status"] == "CHANGE_VERIFIED"
    assert overlay_action["search_destination_relation"] is False
    assert overlay_action["destination_searchboxes"] == "ONE"
    preexisting_action = cast(dict[str, object], preexisting["operator_action"])
    assert preexisting_action["label"] == "OTHER"
    assert preexisting_action["before_searchboxes"] == "ONE"
    assert preexisting_action["destination_searchboxes"] == "ONE"
    assert preexisting_action["search_state_change_status"] == "NO_CHANGE"
    assert preexisting_action["search_destination_relation"] is False
    manually_opened_action = cast(dict[str, object], manually_opened["operator_action"])
    assert manually_opened_action["label"] == "OTHER"
    assert manually_opened_action["stable_semantic_locator_unique"] is False
    assert manually_opened_action["before_searchboxes"] == "ZERO"
    assert manually_opened_action["destination_searchboxes"] == "ONE"
    assert manually_opened_action["search_state_change_status"] == "CHANGE_VERIFIED"
    assert manually_opened_action["search_destination_relation"] is False
    encoded = json.dumps(
        (search, detail, duplicate_search, overlay, preexisting, manually_opened),
        sort_keys=True,
    )
    for private_value in (
        _PRIVATE_USERNAME,
        _PRIVATE_QUERY,
        _PRIVATE_POST_TEXT,
        action_post_id,
        action_candidate,
        detail_url,
        detail_path,
    ):
        assert private_value not in encoded


def test_synthetic_chromium_waits_for_delayed_hydration_and_stable_semantics(
    tmp_path: Path,
) -> None:
    delayed_url = f"{_SYNTHETIC_ORIGIN}/search?q=synthetic"
    delayed_html = """
      <nav role="navigation"><button aria-label="Search">Search</button></nav>
      <script>
        setTimeout(() => {
          const main = document.createElement('main');
          const article = document.createElement('article');
          article.append(document.createElement('header'));
          const paragraph = document.createElement('p');
          paragraph.textContent = 'synthetic fixture';
          article.append(paragraph);
          main.append(article);
          document.body.append(main);
        }, 550);
      </script>
    """

    async def observe_delayed_page() -> tuple[UiDiagnosticSnapshot, float]:
        profile = tmp_path / "synthetic-delayed-hydration-profile"
        profile.mkdir()
        request = BrowserLaunchRequest(
            profile_directory=profile,
            network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            headless=True,
        )
        engine = PlaywrightBrowserEngine()
        try:
            raw_session = await engine.open(request)
        except BrowserRuntimeUnavailable as error:
            if error.code in {"BROWSER_BINARY_MISSING", "BROWSER_START_FAILED"}:
                pytest.skip("Playwright Chromium is not available in this environment")
            raise
        session = cast(_SyntheticUiDiagnosticSession, raw_session)
        try:
            await session.install_synthetic_fixture_pages(
                {
                    f"{_SYNTHETIC_ORIGIN}/": "<main>synthetic Home</main>",
                    delayed_url: delayed_html,
                },
                origin=_SYNTHETIC_ORIGIN,
            )
            await session.navigate(
                delayed_url,
                allowed_origins=frozenset({_SYNTHETIC_ORIGIN}),
            )
            started = asyncio.get_running_loop().time()
            snapshot = await wait_for_stable_ui_snapshot(
                cast(BrowserUiDiagnosticSession, session),
                stage="search",
                expected_username=_PRIVATE_USERNAME,
                configured_topic=_PRIVATE_QUERY,
            )
            elapsed = asyncio.get_running_loop().time() - started
            return snapshot, elapsed
        finally:
            await session.close()

    snapshot, elapsed = asyncio.run(observe_delayed_page())

    assert snapshot.document_ready is True
    assert snapshot.route_class == "SEARCH"
    assert dict(snapshot.counts)["main_landmarks"] == "ONE"
    assert elapsed >= 1.2
    encoded = json.dumps(snapshot.to_document(), sort_keys=True)
    for private_value in (_PRIVATE_USERNAME, _PRIVATE_QUERY, delayed_url, "synthetic"):
        assert private_value not in encoded


class _DiagnosticSession:
    def __init__(self, snapshot: UiDiagnosticSnapshot) -> None:
        self.snapshot = snapshot
        self.navigations: list[tuple[str, frozenset[str]]] = []
        self.observations: list[tuple[str, str]] = []
        self.closed = False
        self.route_class = "HOME"
        self.failures: list[BrowserAdapterError] = []
        self.close_observer: Callable[[], None] | None = None
        self.armed_routes: list[str] = []
        self.arm_failures: list[BrowserAdapterError] = []

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigations.append((url, allowed_origins))

    async def inspect_ui_diagnostic(
        self,
        *,
        expected_username: str,
        configured_topic: str,
    ) -> UiDiagnosticSnapshot:
        self.observations.append((expected_username, configured_topic))
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        return replace(self.snapshot, route_class=self.route_class)

    async def arm_ui_diagnostic_operator_click(self, *, source_route: str) -> None:
        self.armed_routes.append(source_route)
        if self.arm_failures:
            raise self.arm_failures.pop(0)

    async def close(self) -> None:
        self.closed = True
        if self.close_observer is not None:
            self.close_observer()


class _DiagnosticEngine:
    def __init__(self) -> None:
        self.sessions: list[_DiagnosticSession] = []
        self.requests: list[BrowserLaunchRequest] = []
        self.next_session: _DiagnosticSession | None = None

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        self.requests.append(request)
        session = self.next_session
        if session is None:
            session = _DiagnosticSession(validate_ui_diagnostic_snapshot(_valid_raw_snapshot()))
        self.next_session = None
        self.sessions.append(session)
        return cast(BrowserEngineSession, session)


def test_operator_click_capture_errors_offer_bounded_retry_before_stage() -> None:
    session = _DiagnosticSession(validate_ui_diagnostic_snapshot(_valid_raw_snapshot()))
    session.arm_failures.append(BrowserAdapterError("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE"))
    prompts: list[str] = []

    async def choose_retry(
        prompt: str,
        choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        prompts.append(prompt)
        assert choices == frozenset({"RETRY", "SKIP", "STOP"})
        return "RETRY"

    output = io.StringIO()
    status = asyncio.run(
        _arm_operator_action_capture(
            session,
            stage="SEARCH",
            source_route="HOME",
            waiter=choose_retry,
            output=output,
        )
    )

    assert status == "ARMED"
    assert session.armed_routes == ["HOME", "HOME"]
    assert len(prompts) == 1
    assert (
        "DIAGNOSTIC_ERROR stage=SEARCH code=BROWSER_UI_DIAGNOSTIC_UNAVAILABLE" in output.getvalue()
    )
    assert "DIAGNOSTIC_RECOVERY stage=SEARCH" in output.getvalue()


def _account_runtime(
    tmp_path: Path,
) -> tuple[LocalRuntime, LocalAccount, BrowserNurtureConfigV1, _DiagnosticEngine]:
    root = tmp_path / "synthetic-diagnostic-root"
    root.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(root, 0o700)
    accounts = LocalAccountStore(root)
    account = accounts.add("diagnostic_operator")
    profile = root / "profiles" / str(account.id)
    profile.mkdir(parents=True)
    state = BrowserNurtureStateStore(root)
    config = _config(account.id)
    state.save_config(config)
    checkpoint = BrowserNurtureCheckpointV2(
        account_uuid=account.id,
        policy_fingerprint=browser_nurture_policy_fingerprint(config),
        completed_cycles=3,
    )
    state.save_checkpoint(checkpoint)
    engine = _DiagnosticEngine()
    return (
        LocalRuntime(root, accounts, browser_engine=cast(BrowserEngine, engine)),
        account,
        config,
        engine,
    )


def test_diagnostic_holds_existing_account_lock_and_never_changes_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, account, config, engine = _account_runtime(tmp_path)
    root = tmp_path / "synthetic-diagnostic-root"
    checkpoint_path = root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json"
    checkpoint_before = checkpoint_path.read_bytes()
    competing_runtime = LocalRuntime(
        root, LocalAccountStore(root), browser_engine=cast(BrowserEngine, engine)
    )
    calls: list[str] = []

    def reject_checkpoint_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("diagnostics must not load or commit checkpoints")

    monkeypatch.setattr(BrowserNurtureStateStore, "load_checkpoint", reject_checkpoint_load)

    async def waiter(
        prompt: str,
        choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        calls.append(prompt)
        with pytest.raises(StandaloneRuntimeError, match="ACCOUNT_BUSY"):
            async with competing_runtime.own_browser_profile(account.alias):
                pytest.fail("a second browser session acquired the same profile")
        await asyncio.sleep(0.01)
        if "Home checkpoint" in prompt or "Search checkpoint" in prompt:
            engine.sessions[0].route_class = "HOME"
        elif "Detail checkpoint" in prompt:
            engine.sessions[0].route_class = "OTHER"
        assert "OBSERVE" in choices
        return "OBSERVE"

    output = io.StringIO()
    report = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=waiter, output=output)
    )

    assert report["status"] == "COMPLETE"
    assert report["schema"] == "THREADS_UI_DIAGNOSTIC_V2"
    assert engine.sessions[0].armed_routes == ["HOME", "HOME"]
    assert _report_stage(report, "home")["stage_context"] == "ROUTE_MATCH"
    search_report = _report_stage(report, "search")
    detail_report = _report_stage(report, "detail")
    assert search_report["operator_action_capture"] == "ARMED"
    assert detail_report["operator_action_capture"] == "ARMED"
    assert search_report["route_class"] == "HOME"
    assert search_report["stage_context"] == "UNVERIFIED"
    assert detail_report["route_class"] == "OTHER"
    assert detail_report["stage_context"] == "UNVERIFIED"
    assert len(calls) == 4
    assert len(engine.sessions) == 1
    assert len(engine.sessions[0].observations) == 3 * 4
    assert engine.sessions[0].closed
    assert len(engine.requests) == 1
    assert engine.requests[0].headless is False
    assert engine.requests[0].profile_directory == root / "profiles" / str(account.id)
    assert all(
        origin == frozenset({BROWSER_FEED_ORIGIN}) for _, origin in engine.sessions[0].navigations
    )
    assert "DIAGNOSTIC_CLOSE code=OPERATOR_CONFIRMED" in output.getvalue()
    assert checkpoint_path.read_bytes() == checkpoint_before


def test_early_stop_and_cancel_release_profile_for_a_fresh_diagnostic(
    tmp_path: Path,
) -> None:
    runtime, account, config, engine = _account_runtime(tmp_path)
    prompt_count = 0

    async def cancel_during_search(
        _prompt: str,
        _choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        nonlocal prompt_count
        prompt_count += 1
        if prompt_count == 1:
            engine.sessions[0].route_class = "HOME"
            return "OBSERVE"
        raise asyncio.CancelledError

    cancelled = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=cancel_during_search)
    )
    assert cancelled["status"] == "STOPPED"
    assert cancelled["reason_code"] == "INTERRUPTED"
    assert _report_stage(cancelled, "home")["status"] == "OBSERVED"
    assert _report_stage(cancelled, "search")["status"] == "NOT_OBSERVED"
    assert engine.sessions[0].closed

    async def stop_immediately(
        _prompt: str,
        _choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        return "STOP"

    stopped = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=stop_immediately)
    )
    assert stopped["status"] == "STOPPED"
    assert stopped["reason_code"] == "OPERATOR_STOP"
    assert _report_stage(stopped, "home")["status"] == "NOT_OBSERVED"
    assert len(engine.sessions) == 2
    assert engine.sessions[1].closed


def test_recoverable_error_is_printed_before_close_and_retry_keeps_same_locked_session(
    tmp_path: Path,
) -> None:
    runtime, account, config, engine = _account_runtime(tmp_path)
    competing_runtime = LocalRuntime(
        tmp_path / "synthetic-diagnostic-root",
        LocalAccountStore(tmp_path / "synthetic-diagnostic-root"),
        browser_engine=cast(BrowserEngine, engine),
    )
    output = io.StringIO()
    session = _DiagnosticSession(validate_ui_diagnostic_snapshot(_valid_raw_snapshot()))
    session.failures.append(BrowserRuntimeUnavailable("BROWSER_UI_DIAGNOSTIC_UNAVAILABLE"))

    async def waiter(
        prompt: str,
        choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        if "Home checkpoint" in prompt:
            session.route_class = "HOME"
            return "OBSERVE"
        if "Recoverable diagnostic issue" in prompt:
            current_output = output.getvalue()
            assert (
                "DIAGNOSTIC_ERROR stage=HOME code=BROWSER_UI_DIAGNOSTIC_UNAVAILABLE"
                in current_output
            )
            assert not session.closed
            assert choices == frozenset({"RETRY", "SKIP", "STOP"})
            with pytest.raises(StandaloneRuntimeError, match="ACCOUNT_BUSY"):
                async with competing_runtime.own_browser_profile(account.alias):
                    pytest.fail("retry must keep the same profile lock")
            return "RETRY"
        if "Search checkpoint" in prompt:
            session.route_class = "SEARCH"
        elif "Detail checkpoint" in prompt:
            session.route_class = "POST"
        else:
            assert "DIAGNOSTIC report is ready" not in prompt
        assert "OBSERVE" in choices
        return "OBSERVE"

    engine.next_session = session
    session.close_observer = lambda: None
    report = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=waiter, output=output)
    )
    assert report["status"] == "COMPLETE"
    assert engine.sessions == [session]
    assert len(session.observations) == 13
    assert session.closed
    encoded = output.getvalue()
    assert "DIAGNOSTIC_RECOVERY stage=HOME" in encoded
    assert encoded.index("DIAGNOSTIC_ERROR stage=HOME") < encoded.index(
        "DIAGNOSTIC_RECOVERY stage=HOME"
    )
    assert encoded.index('"status": "COMPLETE"') < encoded.index("DIAGNOSTIC_CLOSE_WAIT")
    for private_value in (_PRIVATE_USERNAME, _PRIVATE_QUERY, _PRIVATE_POST_ID, _PRIVATE_POST_TEXT):
        assert private_value not in encoded


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (BrowserProcessCrashed(), "BROWSER_PROCESS_CRASHED"),
        (RemoteSessionStateUncertain(), "REMOTE_STATE_UNCERTAIN"),
        (BrowserAdapterError("PRIVATE_ACCOUNT_KEY_823"), "DIAGNOSTIC_FAILED"),
    ],
)
def test_crash_and_security_errors_stop_without_retry_or_close_ack(
    tmp_path: Path,
    failure: BrowserAdapterError,
    expected_code: str,
) -> None:
    runtime, account, config, engine = _account_runtime(tmp_path)
    session = _DiagnosticSession(validate_ui_diagnostic_snapshot(_valid_raw_snapshot()))
    session.failures.append(failure)
    engine.next_session = session
    output = io.StringIO()
    closed_output: list[str] = []
    session.close_observer = lambda: closed_output.append(output.getvalue())

    async def waiter(
        prompt: str,
        _choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        assert "Home checkpoint" in prompt
        session.route_class = "HOME"
        return "OBSERVE"

    report = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=waiter, output=output)
    )
    assert report["status"] == "ERROR"
    assert report["reason_code"] == expected_code
    assert session.closed
    assert len(session.observations) == 1
    encoded = output.getvalue()
    assert f"DIAGNOSTIC_ERROR stage=HOME code={expected_code}" in encoded
    assert "DIAGNOSTIC_RECOVERY" not in encoded
    assert "DIAGNOSTIC_CLOSE_WAIT" not in encoded
    assert closed_output and f"DIAGNOSTIC_ERROR stage=HOME code={expected_code}" in closed_output[0]
    if expected_code == "DIAGNOSTIC_FAILED":
        assert "PRIVATE_ACCOUNT_KEY_823" not in encoded


def test_loading_timeout_offers_skip_then_stops_with_bounded_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threads_platform.standalone.browser_ui_diagnostic as diagnostic

    monkeypatch.setattr(diagnostic, "_RENDER_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(diagnostic, "_RENDER_SAMPLE_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(diagnostic, "_CLOSE_ACK_WAIT_SECONDS", 0.01)
    runtime, account, config, engine = _account_runtime(tmp_path)
    session = _DiagnosticSession(
        validate_ui_diagnostic_snapshot({**_valid_raw_snapshot(), "document_ready": False})
    )
    engine.next_session = session
    output = io.StringIO()
    closed_output: list[str] = []
    session.close_observer = lambda: closed_output.append(output.getvalue())

    async def waiter(
        prompt: str,
        choices: frozenset[Literal["OBSERVE", "RETRY", "SKIP", "STOP"]],
    ) -> Literal["OBSERVE", "RETRY", "SKIP", "STOP"]:
        if "Home checkpoint" in prompt:
            session.route_class = "HOME"
            return "OBSERVE"
        if "Recoverable diagnostic issue UI_STILL_LOADING" in prompt:
            assert choices == frozenset({"RETRY", "SKIP", "STOP"})
            return "SKIP"
        if "Search checkpoint" in prompt:
            return "STOP"
        if "Diagnostic report is ready" in prompt:
            await asyncio.sleep(1)
        return "OBSERVE"

    report = asyncio.run(
        run_browser_ui_diagnostic(runtime, account.alias, config, waiter=waiter, output=output)
    )
    assert report["status"] == "STOPPED"
    assert _report_stage(report, "home") == {
        "status": "SKIPPED",
        "reason_code": "UI_STILL_LOADING",
    }
    assert report["reason_code"] == "OPERATOR_STOP"
    assert session.closed
    assert "DIAGNOSTIC_ERROR stage=HOME code=UI_STILL_LOADING" in output.getvalue()
    assert "DIAGNOSTIC_CLOSE code=CLOSE_ACK_TIMEOUT" in output.getvalue()
    assert closed_output and "DIAGNOSTIC_CLOSE code=CLOSE_ACK_TIMEOUT" in closed_output[0]


def test_operator_waiter_keeps_async_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threads_platform.standalone.browser_ui_diagnostic as diagnostic

    async def exercise() -> None:
        ticks = 0
        release = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not release.is_set():
                await asyncio.sleep(0.005)
                ticks += 1

        fake_console = types.ModuleType("msvcrt")
        keys = iter(("r", "\r"))
        fake_console.__dict__.update({"kbhit": lambda: ticks >= 5, "getwch": lambda: next(keys)})

        monkeypatch.setattr(diagnostic.os, "name", "nt")
        monkeypatch.setitem(sys.modules, "msvcrt", fake_console)
        task = asyncio.create_task(ticker())
        recovery_choice = await diagnostic.wait_for_diagnostic_choice(
            "synthetic recovery prompt\n",
            frozenset({"RETRY", "SKIP", "STOP"}),
            output=io.StringIO(),
        )
        close_choice = await diagnostic.wait_for_diagnostic_choice(
            "synthetic prompt\n",
            frozenset({"OBSERVE", "STOP"}),
            output=io.StringIO(),
        )
        assert recovery_choice == "RETRY"
        assert close_choice == "OBSERVE"
        release.set()
        await task
        assert ticks >= 5

    asyncio.run(exercise())


def test_diagnostic_cli_rejects_synthetic_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(tmp_path))
    monkeypatch.setattr(cli, "_browser_nurture_interactive_console", lambda: True)
    LocalAccountStore(tmp_path).add("Test-diagnostic")

    assert (
        cli.main(
            [
                "browser-nurture",
                "inspect-ui",
                "Test-diagnostic",
                "--confirm-local-read-only",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ERROR LIVE_ACCOUNT_REQUIRED\n"
    assert "Test-diagnostic" not in captured.err


def test_diagnostic_cli_requires_explicit_confirmation_before_account_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(tmp_path))
    assert cli.main(["browser-nurture", "inspect-ui", "private-alias"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ERROR LOCAL_READ_CONFIRMATION_REQUIRED\n"
    assert "private-alias" not in captured.err
