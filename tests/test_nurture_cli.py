from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.app as app_module
from threads_platform.api.contracts import (
    THREAD_POST_INSIGHT_ORDER,
    DiscoveryPage,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    ThreadPostInsightMetric,
    ThreadPostInsights,
)
from threads_platform.config import Settings
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.mutations import LocalThreadsMutationRuntime


class _SyntheticThreadsAPI:
    def __init__(self, root: Path, account_id: str) -> None:
        self.root = root
        self.account_id = account_id
        self.calls: list[str] = []
        self.publish_calls = 0

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        self.calls.append("mentions")
        return DiscoveryPage((), None, False)

    async def search_threads(
        self,
        token: SecretStr,
        query: str,
        *,
        search_mode: object,
        search_type: object,
        after: str | None,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        self.calls.append("search")
        return DiscoveryPage((), None, False)

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        self._check_token(token)
        self.calls.append("quota")
        return PublishingQuota(usage=0, total=100)

    async def create_container(
        self,
        token: SecretStr,
        request: MediaContainerRequest,
    ) -> MediaContainer:
        self._check_token(token)
        self.calls.append("create")
        assert request.media_type == "TEXT"
        assert request.text == "Synthetic Nurture draft content."
        state_path = self.root / "nurture" / "content" / self.account_id / "recruitment.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert [item["publication_state"] for item in state["candidates"]] == ["RESERVED"]
        return MediaContainer("container-123")

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        self._check_token(token)
        assert container_id == "container-123"
        self.calls.append("publish")
        self.publish_calls += 1
        return "media-123"

    async def get_post_insights(self, token: SecretStr, media_id: str) -> ThreadPostInsights:
        self._check_token(token)
        assert media_id == "media-123"
        self.calls.append("post_insights")
        return ThreadPostInsights(
            media_id=media_id,
            period="lifetime",
            metrics=tuple(
                ThreadPostInsightMetric(name, value)
                for name, value in zip(THREAD_POST_INSIGHT_ORDER, (3, 2, 1, 0), strict=True)
            ),
        )

    @staticmethod
    def _check_token(token: SecretStr) -> None:
        assert token.get_secret_value() == "synthetic-token-for-tests"


class _SyntheticCredentialResolver:
    async def resolve(self, credential_ref: str) -> SecretStr:
        assert credential_ref == "env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC"
        return SecretStr("synthetic-token-for-tests")


def test_wheel_smoke_nurture_observe_apply_and_insights(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """Run all Nurture CLI modes with fake API and mutation effects only."""
    import os

    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(cli_module.__file__).resolve()).casefold()

    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    LocalAccountStore(root).set_credential_ref(
        "alice", "env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC"
    )
    api = _SyntheticThreadsAPI(root, str(account.id))
    mutation_constructions = 0
    actual_mutation_runtime = app_module.LocalThreadsMutationRuntime

    @asynccontextmanager
    async def fake_http_client(_settings: Settings) -> AsyncGenerator[object]:
        yield object()

    def fake_api_client(_client: object) -> _SyntheticThreadsAPI:
        return api

    def tracked_mutation_runtime(*args: Any, **kwargs: Any) -> LocalThreadsMutationRuntime:
        nonlocal mutation_constructions
        mutation_constructions += 1
        return actual_mutation_runtime(*args, **kwargs)

    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", lambda: root)
    monkeypatch.setattr(app_module, "build_threads_http_client", fake_http_client)
    monkeypatch.setattr(app_module, "HttpThreadsAPI", fake_api_client)
    monkeypatch.setattr(
        app_module,
        "EnvironmentThreadsCredentialSecretResolver",
        _SyntheticCredentialResolver,
    )
    monkeypatch.setattr(app_module, "LocalThreadsMutationRuntime", tracked_mutation_runtime)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 0
    observe_output = capsys.readouterr().out
    assert "outcome=SUCCESS" in observe_output
    assert "decision=NO_ACTION" in observe_output
    assert mutation_constructions == 0
    assert api.publish_calls == 0

    draft_file = tmp_path / "synthetic-content.json"
    draft_file.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "POST_TEXT",
                "candidate_id": str(uuid4()),
                "account": "alice",
                "preset": "recruitment",
                "source_kind": "OPERATOR_SOURCE_ID",
                "source_id": "synthetic-source-1",
                "category": "CAREER_TIP",
                "text": "Synthetic Nurture draft content.",
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--post-file",
                str(draft_file),
            ]
        )
        == 0
    )
    apply_output = capsys.readouterr().out
    assert "decision=CONTENT_PUBLISHED" in apply_output
    assert "Synthetic Nurture draft content." not in apply_output
    assert mutation_constructions == 1
    assert api.publish_calls == 1

    operation_paths = tuple((root / "operations").glob("*.json"))
    assert len(operation_paths) == 1
    operation = json.loads(operation_paths[0].read_text(encoding="utf-8"))
    assert operation["kind"] == "POST_TEXT"
    assert operation["phase"] == "PUBLISHED"
    assert operation["media_id"] == "media-123"

    assert (
        cli_module.main(["nurture", "insights", "refresh", "alice", "--preset", "recruitment"]) == 0
    )
    insights_output = capsys.readouterr().out
    assert "nurture-insights refreshed=1" in insights_output
    assert mutation_constructions == 1
    assert api.publish_calls == 1
    assert api.calls.count("post_insights") == 1
