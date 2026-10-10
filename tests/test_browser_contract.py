from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from threads_platform.browser.capabilities import (
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.browser.contracts import (
    BrowserAdapterError,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserProxyCredentials,
    ChallengeDetected,
    SessionExpired,
)
from threads_platform.locks.process_lock import ProcessAlreadyRunning


def test_browser_result_contracts_are_versioned_bounded_and_strict() -> None:
    item = BrowserFeedItemResultV1(
        thread_ref="https://www.threads.com/@synthetic/post/fake_1",
        author_username="synthetic",
        text_excerpt=None,
        position=0,
    )
    result = BrowserFeedResultV1(observations=(item,), truncated=False)
    target = BrowserTargetOpenResultV1(
        target_kind="PROFILE",
        target_ref="/@synthetic",
    )

    assert result.result_version == target.result_version == 1
    assert result.observations == (item,)
    assert target.recognized is True
    with pytest.raises(ValidationError):
        BrowserFeedItemResultV1(thread_ref="x", position=20)
    with pytest.raises(ValidationError):
        BrowserFeedItemResultV1(thread_ref="x", position=0, unexpected="value")
    with pytest.raises(ValidationError):
        BrowserFeedResultV1(observations=tuple(item for _ in range(21)), truncated=True)


def test_launch_request_defaults_to_headed_and_is_immutable() -> None:
    request = BrowserLaunchRequest(
        profile_directory=Path("synthetic-profile"),
        network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
    )

    assert request.headless is False
    with pytest.raises(FrozenInstanceError):
        request.headless = True  # type: ignore[misc]


def test_proxy_secrets_and_browser_error_details_are_not_repr_or_message_data() -> None:
    credentials = BrowserProxyCredentials(
        username="SYNTHETIC_PROXY_USER",
        password="SYNTHETIC_PROXY_SECRET",
    )
    assert "SYNTHETIC_PROXY" not in repr(credentials)

    for error in (
        ChallengeDetected(),
        SessionExpired(),
        ProcessAlreadyRunning("synthetic lock collision"),
        BrowserAdapterError("BROWSER_RUNTIME_UNAVAILABLE"),
    ):
        assert "SYNTHETIC_PROXY" not in str(error)
        assert "SYNTHETIC_PROXY" not in repr(error)
