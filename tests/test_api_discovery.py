from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx2
import pytest
from pydantic import SecretStr

from threads_platform.api.client import HttpThreadsAPI
from threads_platform.api.contracts import (
    DiscoverySearchMode,
    DiscoverySearchType,
    ThreadsContractError,
)

_TOKEN = SecretStr("SYNTHETIC_ONLY_TOKEN_FOR_DISCOVERY_TESTS")


def _call(handler, operation):
    async def invoke():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://graph.threads.net/v1.0/",
        ) as client:
            return await operation(HttpThreadsAPI(client))

    return asyncio.run(invoke())


def _discovery_payload(cursor: str = "synthetic_next_cursor") -> dict[str, object]:
    return {
        "data": [
            {
                "id": "synthetic_thread",
                "media_type": "TEXT",
                "permalink": "https://example.invalid/thread",
                "username": "synthetic_user",
                "text": "Synthetic fixture only",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "is_quote_post": False,
                "has_replies": True,
            }
        ],
        "paging": {"cursors": {"after": cursor}},
    }


def test_search_preserves_mode_type_date_range_and_paging_contract() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_discovery_payload())

    result = _call(
        handler,
        lambda api: api.search_threads(
            _TOKEN,
            "synthetic query",
            search_mode=DiscoverySearchMode.KEYWORD,
            search_type=DiscoverySearchType.RECENT,
            after="synthetic_prior_cursor",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            until=datetime(2025, 12, 31, tzinfo=UTC),
            limit=7,
        ),
    )

    assert len(result.threads) == 1
    assert result.threads[0].remote_thread_id == "synthetic_thread"
    assert result.threads[0].username == "synthetic_user"
    assert result.threads[0].timestamp == datetime(2026, 1, 1, tzinfo=UTC)
    assert result.next_cursor == "synthetic_next_cursor"
    assert result.has_more is True
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path.endswith("/keyword_search")
    assert request.url.params["q"] == "synthetic query"
    assert request.url.params["search_mode"] == "KEYWORD"
    assert request.url.params["search_type"] == "RECENT"
    assert request.url.params["after"] == "synthetic_prior_cursor"
    assert request.url.params["limit"] == "7"
    assert request.url.params["since"] == "2025-01-01T00:00:00+00:00"
    assert request.url.params["until"] == "2025-12-31T00:00:00+00:00"
    assert request.url.params["fields"] == (
        "id,media_type,permalink,username,text,timestamp,is_quote_post,has_replies"
    )


def test_profile_posts_preserves_username_limit_and_after() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_discovery_payload())

    result = _call(
        handler,
        lambda api: api.get_profile_posts(
            _TOKEN, "synthetic_user", after="synthetic_after", limit=12
        ),
    )

    assert result.has_more is True
    assert seen[0].url.path.endswith("/profile_posts")
    assert seen[0].url.params["username"] == "synthetic_user"
    assert seen[0].url.params["after"] == "synthetic_after"
    assert seen[0].url.params["limit"] == "12"


def test_mentions_continuation_is_forwarded_and_cursor_returned() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_discovery_payload("synthetic_followup_cursor"))

    result = _call(
        handler,
        lambda api: api.get_mentions(
            _TOKEN,
            after="synthetic_page_one_cursor",
            since=None,
            until=None,
            limit=25,
        ),
    )

    assert result.next_cursor == "synthetic_followup_cursor"
    assert result.has_more is True
    assert seen[0].url.path.endswith("/me/mentions")
    assert seen[0].url.params["after"] == "synthetic_page_one_cursor"
    assert seen[0].url.params["limit"] == "25"


@pytest.mark.parametrize("edge", ["replies", "conversation"])
def test_reply_and_conversation_edges_keep_fields_reverse_and_cursor(edge: str) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "id": "synthetic_reply",
                        "text": "Synthetic reply text",
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "root_post": {"id": "synthetic_root"},
                        "replied_to": {"id": "synthetic_parent"},
                        "is_reply_owned_by_me": False,
                    }
                ],
                "paging": {
                    "next": "https://example.invalid/next",
                    "cursors": {"after": "synthetic_reply_cursor"},
                },
            },
        )

    method = "get_replies" if edge == "replies" else "get_conversation"
    result = _call(
        handler,
        lambda api: getattr(api, method)(_TOKEN, "synthetic_thread", "synthetic_before"),
    )

    assert result.replies[0].reply_id == "synthetic_reply"
    assert result.replies[0].root_post_id == "synthetic_root"
    assert result.replies[0].replied_to_id == "synthetic_parent"
    assert result.next_cursor == "synthetic_reply_cursor"
    assert result.has_more is True
    assert seen[0].url.path.endswith(f"/synthetic_thread/{edge}")
    assert seen[0].url.params["after"] == "synthetic_before"
    assert seen[0].url.params["reverse"] == "false"
    assert seen[0].url.params["fields"] == (
        "id,text,timestamp,root_post,replied_to,is_reply_owned_by_me"
    )


def test_invalid_discovery_cursor_contract_fails_closed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_discovery_payload("\n"))

    with pytest.raises(ThreadsContractError):
        _call(
            handler,
            lambda api: api.get_profile_posts(_TOKEN, "synthetic_user", after=None, limit=25),
        )
