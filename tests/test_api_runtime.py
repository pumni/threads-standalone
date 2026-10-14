from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from threads_platform.api.contracts import (
    PublishingQuota,
    RemoteAuthenticatedProfile,
    ThreadsAPI,
    ThreadsAPIError,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError

_CREDENTIAL_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_SPLIT02_TEST_V1"
_SYNTHETIC_TOKEN = "SYNTHETIC_TOKEN_SENTINEL_FOR_TESTS_ONLY"


class _Resolver:
    def __init__(self) -> None:
        self.references: list[str] = []
        self.secret = SecretStr(_SYNTHETIC_TOKEN)

    async def resolve(self, credential_ref: str) -> SecretStr:
        self.references.append(credential_ref)
        return self.secret


class _Api:
    def __init__(self) -> None:
        self.quota_calls: list[SecretStr] = []
        self.authenticated_profile_calls: list[SecretStr] = []
        self.failure: Exception | None = None

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        self.quota_calls.append(token)
        if self.failure is not None:
            raise self.failure
        return PublishingQuota(usage=3, total=250, reply_usage=1, reply_total=100)

    async def get_authenticated_profile(self, token: SecretStr) -> RemoteAuthenticatedProfile:
        self.authenticated_profile_calls.append(token)
        if self.failure is not None:
            raise self.failure
        return RemoteAuthenticatedProfile("synthetic_user_id", "synthetic_owner")


def _runtime(root: Path) -> tuple[LocalThreadsApiRuntime, _Api, _Resolver]:
    accounts = LocalAccountStore(root)
    accounts.add("synthetic_account")
    accounts.set_credential_ref("synthetic_account", _CREDENTIAL_REF)
    api = _Api()
    resolver = _Resolver()
    return (
        LocalThreadsApiRuntime(accounts, cast(ThreadsAPI, api), resolver),
        api,
        resolver,
    )


def test_quota_resolves_the_reference_on_each_invocation(tmp_path: Path) -> None:
    runtime, api, resolver = _runtime(tmp_path)

    first = asyncio.run(runtime.quota("synthetic_account"))
    second = asyncio.run(runtime.quota("synthetic_account"))

    assert first == second == PublishingQuota(usage=3, total=250, reply_usage=1, reply_total=100)
    assert resolver.references == [_CREDENTIAL_REF, _CREDENTIAL_REF]
    assert api.quota_calls == [resolver.secret, resolver.secret]


def test_authenticated_profile_uses_alias_bound_credential(tmp_path: Path) -> None:
    runtime, api, resolver = _runtime(tmp_path)

    profile = asyncio.run(runtime.authenticated_profile("synthetic_account"))

    assert profile == RemoteAuthenticatedProfile("synthetic_user_id", "synthetic_owner")
    assert resolver.references == [_CREDENTIAL_REF]
    assert api.authenticated_profile_calls == [resolver.secret]
    assert "synthetic_user_id" not in repr(profile)
    assert "synthetic_owner" not in repr(profile)


@pytest.mark.parametrize("command", ["replies", "mentions"])
def test_invalid_cursor_fails_before_account_lookup(command: str) -> None:
    class _NoLookup:
        def get(self, alias: str) -> object:
            raise AssertionError("invalid input must fail before account lookup")

    runtime = LocalThreadsApiRuntime(
        cast(LocalAccountStore, _NoLookup()), cast(ThreadsAPI, _Api()), _Resolver()
    )

    with pytest.raises(StandaloneApiError) as error:
        if command == "replies":
            asyncio.run(runtime.replies("missing", "thread-synthetic", after="\n"))
        else:
            asyncio.run(runtime.mentions("missing", after="\n"))

    assert error.value.code == "INVALID_CURSOR"


def test_api_error_does_not_include_the_resolved_token(tmp_path: Path) -> None:
    runtime, api, resolver = _runtime(tmp_path)
    api.failure = ThreadsAPIError("THREADS_TRANSPORT_FAILURE")

    with pytest.raises(ThreadsAPIError) as error:
        asyncio.run(runtime.quota("synthetic_account"))

    assert error.value.code == "THREADS_TRANSPORT_FAILURE"
    assert _SYNTHETIC_TOKEN not in str(error.value)
    assert _SYNTHETIC_TOKEN not in repr(error.value)
    assert resolver.references == [_CREDENTIAL_REF]


# Temporary API_ONLY hosted workflow validation; no behavior change.
