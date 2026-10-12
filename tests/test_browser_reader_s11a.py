from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from html import escape
from urllib.parse import urlencode
from uuid import UUID

import pytest

from threads_platform.browser import playwright_engine
from threads_platform.browser.contracts import (
    BrowserAdapterError,
    BrowserContractError,
    BrowserProcessCrashed,
    BrowserReadTimeout,
    BrowserSessionMismatch,
    ChallengeDetected,
    LocatorNotFound,
    RemoteSessionStateUncertain,
    SessionExpired,
    UnsupportedUIState,
)
from threads_platform.browser.read_adapters import (
    SyntheticBrowserConversationAdapter,
    SyntheticBrowserSearchAdapter,
)
from threads_platform.browser.synthetic_html_readers import (
    SyntheticBrowserSessionContext,
    SyntheticHtmlBrowserReader,
    SyntheticHtmlDocument,
)

_ACCOUNT_ONE = UUID("00000000-0000-4000-8000-000000000001")
_ACCOUNT_TWO = UUID("00000000-0000-4000-8000-000000000002")
_QUERY = "design role"
_CONTEXT_ONE = SyntheticBrowserSessionContext(_ACCOUNT_ONE)
_ROOT = "https://www.threads.com/@synthetic/post/root_1"


def _post(
    username: str,
    post_id: str,
    *,
    text: str | None = None,
    parent_ref: str | None = None,
    content_truncated: bool = False,
) -> str:
    text_marker = ""
    if text is not None:
        text_marker = (
            '<p role="paragraph" aria-label="Post text" '
            f'data-truncated="{str(content_truncated).lower()}">{escape(text)}</p>'
        )
    parent_marker = ""
    if parent_ref is not None:
        parent_marker = (
            '<a role="link" aria-label="Replying to" '
            f'href="{escape(parent_ref, quote=True)}">Reply target</a>'
        )
    return (
        '<article role="article">'
        '<a role="link" aria-label="Post permalink" '
        f'href="/@{username}/post/{post_id}">Open post</a>'
        f'<a role="link" aria-label="Author" href="/@{username}/">@{username}</a>'
        f"{text_marker}{parent_marker}</article>"
    )


def _document(
    url: str,
    main: str,
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
    before_main: str = "",
) -> SyntheticHtmlDocument:
    return SyntheticHtmlDocument(
        url=url,
        html=(
            "<html><head>"
            f'<meta name="synthetic-account-id" content="{account_uuid}">'
            "</head><body>"
            f"{before_main}{main}</body></html>"
        ),
    )


def _search_document(
    batches: Sequence[Sequence[str]],
    *,
    query: str = _QUERY,
    account_uuid: UUID = _ACCOUNT_ONE,
    before_main: str = "",
    main_attributes: str = "",
) -> SyntheticHtmlDocument:
    batch_html = "".join(
        f'<section role="region" aria-label="Search result batch">{"".join(posts)}</section>'
        for posts in batches
    )
    main = (
        '<main role="main" aria-label="Search results" '
        f'data-query="{escape(query, quote=True)}" {main_attributes}>'
        f"{batch_html}</main>"
    )
    url = f"https://www.threads.com/search?{urlencode({'q': query})}"
    return _document(url, main, account_uuid=account_uuid, before_main=before_main)


def _conversation_document(
    posts: Sequence[str],
    *,
    target: str = _ROOT,
    page_url: str | None = None,
    context_status: str | None = "complete",
    account_uuid: UUID = _ACCOUNT_ONE,
    before_main: str = "",
) -> SyntheticHtmlDocument:
    context_attribute = (
        f' data-context-status="{escape(context_status, quote=True)}"'
        if context_status is not None
        else ""
    )
    main = (
        '<main role="main" aria-label="Conversation" '
        f'data-target="{escape(target, quote=True)}"{context_attribute}>'
        f"{''.join(posts)}</main>"
    )
    return _document(
        page_url or target,
        main,
        account_uuid=account_uuid,
        before_main=before_main,
    )


def _reader(document: SyntheticHtmlDocument, account_uuid: UUID = _ACCOUNT_ONE):
    return SyntheticHtmlBrowserReader(
        document,
        SyntheticBrowserSessionContext(account_uuid),
    )


def test_synthetic_html_search_handles_zero_and_one_result() -> None:
    empty = asyncio.run(SyntheticBrowserSearchAdapter(_reader(_search_document(()))).search(_QUERY))
    single = asyncio.run(
        SyntheticBrowserSearchAdapter(
            _reader(_search_document(((_post("synthetic", "one_1", text="A design role"),),)))
        ).search(_QUERY)
    )

    assert empty.observations == ()
    assert empty.source == "SYNTHETIC_FIXTURE"
    assert len(single.observations) == 1
    item = single.observations[0]
    assert item.thread_ref == "https://www.threads.com/@synthetic/post/one_1"
    assert item.author_username == "synthetic"
    assert item.text_excerpt == "A design role"
    assert item.position == 0
    assert item.source == single.source == "SYNTHETIC_FIXTURE"
    assert item.context_status == "UNKNOWN"
    assert item.truncated is False


def test_search_caps_many_results_deduplicates_and_preserves_truncation() -> None:
    first_batch = tuple(
        _post("synthetic", f"post_{index}", text=f"fixture {index}") for index in range(20)
    )
    second_batch = (
        first_batch[0],
        _post("synthetic", "post_20", text="fixture twenty"),
    )
    result = asyncio.run(
        SyntheticBrowserSearchAdapter(
            _reader(_search_document((first_batch, second_batch)))
        ).search(_QUERY, max_items=20)
    )

    assert len(result.observations) == 20
    assert [item.position for item in result.observations] == list(range(20))
    assert result.observations[0].thread_ref.endswith("/post_0")
    assert result.truncated is True


def test_search_rejects_requested_and_fixture_bounds() -> None:
    reader = _reader(_search_document(()))
    with pytest.raises(BrowserContractError):
        asyncio.run(SyntheticBrowserSearchAdapter(reader).search(_QUERY, max_items=21))

    six_batches = _search_document(tuple(() for _ in range(6)))
    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(six_batches)).search(_QUERY))

    over_candidates = _search_document(
        (tuple(_post("synthetic", f"many_{index}") for index in range(101)),)
    )
    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(over_candidates)).search(_QUERY))


def test_changed_fixture_structure_and_duplicate_markers_fail_closed() -> None:
    no_main = _document("https://www.threads.com/search?q=design+role", "<main></main>")
    with pytest.raises(LocatorNotFound):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(no_main)).search(_QUERY))

    duplicate_main = _search_document(())
    duplicate_main = SyntheticHtmlDocument(
        duplicate_main.url,
        duplicate_main.html.replace(
            "</body>", '<main role="main" aria-label="Search results"></main></body>'
        ),
    )
    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(duplicate_main)).search(_QUERY))

    duplicate_author = _post("synthetic", "duplicate_1").replace(
        "</article>",
        '<a role="link" aria-label="Author" href="/@synthetic/">@synthetic</a></article>',
    )
    with pytest.raises(UnsupportedUIState):
        asyncio.run(
            SyntheticBrowserSearchAdapter(_reader(_search_document(((duplicate_author,),)))).search(
                _QUERY
            )
        )


def test_off_origin_redirect_and_conflicting_author_are_rejected() -> None:
    off_origin = SyntheticHtmlDocument(
        "https://evil.invalid/search?q=design+role",
        _search_document(()).html,
    )
    with pytest.raises(RemoteSessionStateUncertain):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(off_origin)).search(_QUERY))

    redirected = SyntheticHtmlDocument(
        "https://www.threads.com/login?q=design+role",
        _search_document(()).html,
    )
    with pytest.raises(RemoteSessionStateUncertain):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(redirected)).search(_QUERY))

    conflicting_author = _post("synthetic", "conflict_1").replace(
        'href="/@synthetic/">@synthetic', 'href="/@other/">@other'
    )
    with pytest.raises(UnsupportedUIState):
        asyncio.run(
            SyntheticBrowserSearchAdapter(
                _reader(_search_document(((conflicting_author,),)))
            ).search(_QUERY)
        )


def test_search_marks_explicitly_truncated_excerpt() -> None:
    document = _search_document(
        ((_post("synthetic", "partial_1", text="short prefix", content_truncated=True),),)
    )
    result = asyncio.run(SyntheticBrowserSearchAdapter(_reader(document)).search(_QUERY))

    assert result.truncated is True
    assert result.observations[0].truncated is True
    assert result.observations[0].text_excerpt == "short prefix"


def test_conversation_requires_exact_root_and_preserves_evidenced_nesting() -> None:
    root = _post("synthetic", "root_1", text="root post")
    nested = _post(
        "nested_author",
        "nested_1",
        text="nested reply",
        parent_ref="/@reply_author/post/reply_1",
    )
    reply = _post("reply_author", "reply_1", text="first reply", parent_ref=_ROOT)
    orphan = _post("orphan_author", "orphan_1", text="unknown parent")
    result = asyncio.run(
        SyntheticBrowserConversationAdapter(
            _reader(_conversation_document((root, nested, reply, orphan)))
        ).read(_ROOT)
    )

    assert result.root_thread_ref == _ROOT
    assert result.source == "SYNTHETIC_FIXTURE"
    assert result.context_status == "PARTIAL"
    assert [item.parent_ref for item in result.observations] == [
        None,
        "https://www.threads.com/@reply_author/post/reply_1",
        _ROOT,
        None,
    ]


def test_conversation_reports_partial_complete_and_unknown_context() -> None:
    root = _post("synthetic", "root_1", text="root post")
    partial = asyncio.run(
        SyntheticBrowserConversationAdapter(
            _reader(_conversation_document((root,), context_status="partial"))
        ).read(_ROOT)
    )
    complete = asyncio.run(
        SyntheticBrowserConversationAdapter(
            _reader(_conversation_document((root,), context_status="complete"))
        ).read(_ROOT)
    )
    unknown = asyncio.run(
        SyntheticBrowserConversationAdapter(
            _reader(_conversation_document((root,), context_status=None))
        ).read(_ROOT)
    )

    assert partial.context_status == "PARTIAL"
    assert complete.context_status == "COMPLETE"
    assert unknown.context_status == "UNKNOWN"


def test_conversation_rejects_missing_root_target_mismatch_and_truncated_content() -> None:
    missing_root = _conversation_document((_post("reply_author", "reply_1"),))
    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserConversationAdapter(_reader(missing_root)).read(_ROOT))

    wrong_target = _conversation_document(
        (_post("synthetic", "root_1"),),
        target="/@other/post/x",
        page_url=_ROOT,
    )
    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserConversationAdapter(_reader(wrong_target)).read(_ROOT))

    truncated = _conversation_document(
        (_post("synthetic", "root_1", text="root prefix", content_truncated=True),)
    )
    result = asyncio.run(SyntheticBrowserConversationAdapter(_reader(truncated)).read(_ROOT))
    assert result.truncated is True
    assert result.context_status == "PARTIAL"


def test_conversation_rejects_redirect_and_missing_author() -> None:
    no_author = _post("synthetic", "root_1").replace(
        '<a role="link" aria-label="Author" href="/@synthetic/">@synthetic</a>', ""
    )
    with pytest.raises(LocatorNotFound):
        asyncio.run(
            SyntheticBrowserConversationAdapter(_reader(_conversation_document((no_author,)))).read(
                _ROOT
            )
        )

    redirected_fixture = _conversation_document((_post("synthetic", "root_1"),))
    redirected = SyntheticHtmlDocument(
        "https://www.threads.com/@other/post/other",
        redirected_fixture.html,
    )
    with pytest.raises(RemoteSessionStateUncertain):
        asyncio.run(SyntheticBrowserConversationAdapter(_reader(redirected)).read(_ROOT))


def test_login_challenge_and_cross_account_session_mismatch_fail_closed() -> None:
    challenged = _search_document(
        (), before_main='<div role="alert" aria-label="Login challenge"></div>'
    )
    with pytest.raises(ChallengeDetected):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(challenged)).search(_QUERY))

    expired = _search_document(
        (), before_main='<div role="alert" aria-label="Session expired"></div>'
    )
    with pytest.raises(SessionExpired):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(expired)).search(_QUERY))

    mismatched = _search_document((), account_uuid=_ACCOUNT_ONE)
    with pytest.raises(BrowserSessionMismatch):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(mismatched, _ACCOUNT_TWO)).search(_QUERY))


class _FailedSearchReader:
    def __init__(self, error: BrowserAdapterError) -> None:
        self.error = error

    async def search_candidates(
        self,
        query: str,
        *,
        iteration_bound: int,
        candidate_bound: int,
    ) -> Sequence[Sequence[Mapping[str, object]]]:
        raise self.error


class _SlowSearchReader:
    async def search_candidates(
        self,
        query: str,
        *,
        iteration_bound: int,
        candidate_bound: int,
    ) -> Sequence[Sequence[Mapping[str, object]]]:
        await asyncio.sleep(0.05)
        return ()


def test_session_crash_and_timeout_map_to_safe_failure_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(BrowserProcessCrashed):
        asyncio.run(
            SyntheticBrowserSearchAdapter(_FailedSearchReader(BrowserProcessCrashed())).search(
                _QUERY
            )
        )

    monkeypatch.setattr(
        "threads_platform.browser.read_adapters.BROWSER_READ_TIMEOUT_SECONDS", 0.001
    )
    with pytest.raises(BrowserReadTimeout):
        asyncio.run(SyntheticBrowserSearchAdapter(_SlowSearchReader()).search(_QUERY))


def test_synthetic_reader_has_no_browser_or_network_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def forbidden_browser_start() -> None:
        calls.append("browser-start")
        raise AssertionError("synthetic reader attempted browser startup")

    monkeypatch.setattr(playwright_engine, "async_playwright", forbidden_browser_start)
    document = _search_document(((_post("synthetic", "offline_1", text="fixture"),),))
    result = asyncio.run(SyntheticBrowserSearchAdapter(_reader(document)).search(_QUERY))

    assert result.source == "SYNTHETIC_FIXTURE"
    assert calls == []


def test_search_rejects_conflicting_duplicate_post_identity() -> None:
    first = _post("synthetic", "same_1", text="first excerpt")
    conflict = _post("synthetic", "same_1", text="different excerpt")
    document = _search_document(((first,), (conflict,)))

    with pytest.raises(UnsupportedUIState):
        asyncio.run(SyntheticBrowserSearchAdapter(_reader(document)).search(_QUERY))


def test_conversation_rejects_conflicting_duplicate_root() -> None:
    root = _post("synthetic", "root_1", text="root")
    duplicate_root = _post("synthetic", "root_1", text="conflicting root")
    with pytest.raises(UnsupportedUIState):
        asyncio.run(
            SyntheticBrowserConversationAdapter(
                _reader(_conversation_document((root, duplicate_root)))
            ).read(_ROOT)
        )
