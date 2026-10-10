from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx2
import pytest
from pydantic import SecretStr

from threads_platform.api.client import HttpThreadsAPI
from threads_platform.api.contracts import (
    THREAD_POST_INSIGHT_ORDER,
    ThreadsAPIError,
    ThreadsContractError,
    ThreadsTransportError,
)

_TOKEN = SecretStr("SYNTHETIC_ONLY_TOKEN_FOR_HTTP_MOCKS")


def _run(handler: Callable[[httpx2.Request], httpx2.Response], operation: Callable) -> object:
    async def invoke() -> object:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://graph.threads.net/v1.0/",
        ) as client:
            return await operation(HttpThreadsAPI(client))

    return asyncio.run(invoke())


def test_media_request_and_response_contract() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "synthetic_media",
                "text": "synthetic text",
                "permalink": "https://example.invalid/synthetic",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )

    result = _run(handler, lambda api: api.get_media(_TOKEN, "synthetic_media"))

    assert result.media_id == "synthetic_media"
    assert result.text == "synthetic text"
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/synthetic_media")
    assert seen[0].url.params["fields"] == "id,text,permalink,timestamp"
    assert seen[0].headers["Authorization"] == f"Bearer {_TOKEN.get_secret_value()}"


def test_quota_shape_and_fields_are_preserved() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "quota_usage": 3,
                        "config": {"quota_total": 250, "quota_duration": 86400},
                        "reply_quota_usage": 1,
                        "reply_config": {"quota_total": 100, "quota_duration": 86400},
                    }
                ]
            },
        )

    result = _run(handler, lambda api: api.get_publishing_quota(_TOKEN))

    assert result.usage == 3
    assert result.total == 250
    assert result.reply_usage == 1
    assert result.reply_total == 100
    assert result.duration_seconds == 86400
    assert seen[0].url.path.endswith("/me/threads_publishing_limit")
    assert seen[0].url.params["fields"] == "quota_usage,config,reply_quota_usage,reply_config"


def test_post_insights_keeps_metric_order_and_lifetime_shape() -> None:
    seen: list[httpx2.Request] = []
    metrics = [
        {"name": name.value, "period": "lifetime", "values": [{"value": index + 1}]}
        for index, name in enumerate(THREAD_POST_INSIGHT_ORDER)
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": metrics})

    result = _run(handler, lambda api: api.get_post_insights(_TOKEN, "synthetic_media"))

    assert result.media_id == "synthetic_media"
    assert result.period == "lifetime"
    assert tuple(item.name for item in result.metrics) == THREAD_POST_INSIGHT_ORDER
    assert tuple(item.value for item in result.metrics) == (1, 2, 3, 4)
    assert seen[0].url.params["metric"] == "likes,replies,reposts,quotes"
    assert seen[0].url.path.endswith("/synthetic_media/insights")


def test_public_profile_bounds_and_validates_https_picture() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "synthetic_author",
                "username": "synthetic_user",
                "name": "Synthetic User",
                "threads_biography": "Synthetic profile",
                "threads_profile_picture_url": "https://example.invalid/avatar",
            },
        )

    result = _run(handler, lambda api: api.get_public_profile(_TOKEN, "synthetic_user"))

    assert result.remote_author_id == "synthetic_author"
    assert result.username == "synthetic_user"
    assert result.display_name == "Synthetic User"
    assert result.biography == "Synthetic profile"
    assert result.profile_picture_url == "https://example.invalid/avatar"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "THREADS_INVALID_REQUEST"),
        (401, "THREADS_AUTHENTICATION_FAILED"),
        (403, "THREADS_PERMISSION_DENIED"),
        (404, "THREADS_OBJECT_NOT_FOUND"),
        (429, "THREADS_RATE_LIMITED"),
        (503, "THREADS_SERVER_ERROR"),
    ],
)
def test_api_errors_are_normalized_and_do_not_include_response_body(
    status: int, expected: str
) -> None:
    body_marker = "SYNTHETIC_RESPONSE_BODY_MUST_NOT_ESCAPE"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text=body_marker)

    with pytest.raises(ThreadsAPIError) as error:
        _run(handler, lambda api: api.get_publishing_quota(_TOKEN))

    assert error.value.code == expected
    assert body_marker not in str(error.value)
    assert _TOKEN.get_secret_value() not in repr(error.value)


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"data": "wrong-shape"}'])
def test_invalid_response_envelope_fails_closed(payload: bytes) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=payload)

    with pytest.raises(ThreadsContractError) as error:
        _run(handler, lambda api: api.get_publishing_quota(_TOKEN))

    assert error.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


def test_timeout_becomes_sanitized_transport_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("SYNTHETIC_TRANSPORT_DETAIL")

    with pytest.raises(ThreadsTransportError) as error:
        _run(handler, lambda api: api.get_publishing_quota(_TOKEN))

    assert error.value.code == "THREADS_TRANSPORT_FAILURE"
    assert "SYNTHETIC_TRANSPORT_DETAIL" not in str(error.value)


def test_wheel_smoke_quota_read() -> None:
    """Used by CI with the built wheel installed in an isolated environment."""

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"data": [{"quota_usage": 2, "config": {"quota_total": 250}}]},
        )

    result = _run(handler, lambda api: api.get_publishing_quota(_TOKEN))

    assert result.usage == 2
    assert result.total == 250
    assert len(seen) == 1
