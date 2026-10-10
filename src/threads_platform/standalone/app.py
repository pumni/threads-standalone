"""Minimal local composition for accounts and read-only Threads API commands."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from threads_platform.api.client import HttpThreadsAPI
from threads_platform.config import Settings
from threads_platform.credentials.environment import EnvironmentThreadsCredentialSecretResolver
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime, build_threads_http_client
from threads_platform.standalone.mutations import LocalOperationStore, LocalThreadsMutationRuntime


@dataclass(frozen=True, slots=True)
class StandaloneAppContext:
    accounts: LocalAccountStore
    api: LocalThreadsApiRuntime
    mutations: LocalThreadsMutationRuntime


@asynccontextmanager
async def build_standalone_app(
    root: Path,
    accounts: LocalAccountStore | None = None,
) -> AsyncGenerator[StandaloneAppContext]:
    """Build the account/API runtime and own the one-command HTTP client lifetime."""
    local_accounts = accounts if accounts is not None else LocalAccountStore(root)
    async with build_threads_http_client(Settings()) as client:
        http_api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        api = LocalThreadsApiRuntime(
            local_accounts,
            http_api,
            secret_resolver,
        )
        mutations = LocalThreadsMutationRuntime(
            root,
            local_accounts,
            http_api,
            secret_resolver,
            LocalOperationStore(root),
        )
        yield StandaloneAppContext(accounts=local_accounts, api=api, mutations=mutations)
