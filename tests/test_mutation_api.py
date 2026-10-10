from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx2
import pytest
from pydantic import SecretStr

from threads_platform.api.client import HttpThreadsAPI
from threads_platform.api.contracts import (
    MediaContainerRequest,
    ThreadsAPIError,
    ThreadsContractError,
)

_TOKEN = SecretStr("SYNTHETIC_ONLY_TOKEN_FOR_MUTATION_HTTP_MOCKS")


def _run(handler: Callable[[httpx2.Request], httpx2.Response], operation: Callable) -> object:
    async def invoke() -> object:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://graph.threads.net/v1.0/",
        ) as client:
            return await operation(HttpThreadsAPI(client))

    return asyncio.run(invoke())


def test_create_container_sends_quote_reply_media_and_carousel_fields() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "synthetic_container"})

    result = _run(
        handler,
        lambda api: api.create_container(
            _TOKEN,
            MediaContainerRequest(
                media_type="CAROUSEL",
                text="synthetic caption",
                alt_text="synthetic description",
                children=("child_one", "child_two"),
                is_carousel_item=True,
                quote_post_id="synthetic_quote",
                reply_to_id="synthetic_thread",
                reply_control="everyone",
            ),
        ),
    )

    assert result.container_id == "synthetic_container"
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/me/threads")
    assert dict(seen[0].url.params) == {
        "media_type": "CAROUSEL",
        "text": "synthetic caption",
        "alt_text": "synthetic description",
        "quote_post_id": "synthetic_quote",
        "reply_to_id": "synthetic_thread",
        "reply_control": "everyone",
        "children": "child_one,child_two",
        "is_carousel_item": "true",
    }
    assert seen[0].headers["Authorization"] == f"Bearer {_TOKEN.get_secret_value()}"


def test_get_and_publish_container_use_exact_read_and_publish_routes() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "id": "synthetic_container",
                    "status": "FINISHED",
                    "error_message": None,
                },
            )
        return httpx2.Response(200, json={"id": "synthetic_media"})

    async def operation(api: HttpThreadsAPI) -> tuple[object, str]:
        container = await api.get_container(_TOKEN, "synthetic_container")
        media_id = await api.publish_container(_TOKEN, container.container_id)
        return container, media_id

    container, media_id = _run(handler, operation)

    assert (container.container_id, container.status, container.error_message) == (
        "synthetic_container",
        "FINISHED",
        None,
    )
    assert media_id == "synthetic_media"
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/v1.0/synthetic_container"),
        ("POST", "/v1.0/me/threads_publish"),
    ]
    assert seen[0].url.params["fields"] == "id,status,error_message"
    assert seen[1].url.params["creation_id"] == "synthetic_container"


@pytest.mark.parametrize(
    ("method", "route", "parameter", "value"),
    [
        ("manage_reply", "manage_reply", "hide", "true"),
        ("manage_reply", "manage_reply", "hide", "false"),
        ("manage_pending_reply", "manage_pending_reply", "approve", "true"),
        ("manage_pending_reply", "manage_pending_reply", "approve", "false"),
    ],
)
def test_reply_moderation_posts_exact_action_once(
    method: str, route: str, parameter: str, value: str
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"success": True})

    async def operation(api: HttpThreadsAPI) -> None:
        if method == "manage_reply":
            await api.manage_reply(_TOKEN, "synthetic_reply", hide=value == "true")
        else:
            await api.manage_pending_reply(_TOKEN, "synthetic_reply", approve=value == "true")

    assert _run(handler, operation) is None
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith(f"/synthetic_reply/{route}")
    assert seen[0].url.params[parameter] == value


def test_mutation_api_failure_is_sanitized_without_retry() -> None:
    seen: list[httpx2.Request] = []
    body_marker = "SYNTHETIC_PRIVATE_RESPONSE_BODY"

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(503, text=body_marker)

    with pytest.raises(ThreadsAPIError) as error:
        _run(
            handler,
            lambda api: api.publish_container(_TOKEN, "synthetic_container"),
        )

    assert error.value.code == "THREADS_SERVER_ERROR"
    assert body_marker not in str(error.value)
    assert len(seen) == 1


def test_moderation_response_contract_mismatch_is_fail_closed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"success": "yes"})

    with pytest.raises(ThreadsContractError):
        _run(handler, lambda api: api.manage_reply(_TOKEN, "synthetic_reply", hide=True))
