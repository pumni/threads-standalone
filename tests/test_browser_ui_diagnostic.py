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
from typing import Literal, Protocol, cast
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
from threads_platform.browser.playwright_engine import PlaywrightBrowserEngine
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
        "query_match": True,
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
    script = DIAGNOSTIC_SCRIPT.lower()
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
    assert search["query_match"] is True
    assert query_from_url["query_match"] is True
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
    assert detail["body_status"] == "PARTIAL"
    assert any(
        relation["relation"] == "DETAIL_POST_AND_AUTHOR_SHARE_CONTAINER"
        for relation in detail_relations
    )
    assert home_overlay["route_class"] == "HOME"
    assert home_overlay["query_match"] is True
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
    assert _report_stage(report, "home")["stage_context"] == "ROUTE_MATCH"
    search_report = _report_stage(report, "search")
    detail_report = _report_stage(report, "detail")
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
