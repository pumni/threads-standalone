from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.app as app_module
import threads_platform.standalone.runtime as runtime_module
from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
)
from threads_platform.browser.contracts import BROWSER_FEED_ORIGIN, BrowserLaunchRequest
from threads_platform.config import Settings
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.recurrences import LocalRecurrenceRunner, LocalRecurrenceStore
from threads_platform.standalone.workflows import WorkflowPlan, is_read_only_workflow

_TOKEN = "synthetic-split07-token"
_WORKFLOW_TEXT = "synthetic workflow post text"
_FEED_REF = "https://www.threads.com/@synthetic/post/synthetic-feed-id"


class _SyntheticThreadsAPI:
    def __init__(self, data_root: Path) -> None:
        self._operation_directory = data_root / "operations"
        self.read_calls: list[str] = []
        self.mutation_calls: list[str] = []

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        self._check_token(token)
        self.read_calls.append("quota")
        return PublishingQuota(usage=2, total=100, reply_usage=1, reply_total=50)

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        assert after is None and since is None and until is None and limit > 0
        self.read_calls.append("mentions")
        return DiscoveryPage((), None, False)

    async def search_threads(
        self,
        token: SecretStr,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        assert query
        assert search_mode in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
        assert search_type is DiscoverySearchType.RECENT
        assert after is None and since is None and until is None and limit > 0
        self.read_calls.append("search")
        return DiscoveryPage((), None, False)

    async def get_profile_posts(
        self,
        token: SecretStr,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        assert username and after is None and limit > 0
        self.read_calls.append("profile_posts")
        return DiscoveryPage((), None, False)

    async def create_container(
        self,
        token: SecretStr,
        request: MediaContainerRequest,
    ) -> MediaContainer:
        self._check_token(token)
        assert request.media_type == "TEXT"
        assert request.text == _WORKFLOW_TEXT
        self.mutation_calls.append("create_container")
        return MediaContainer("synthetic-container-id")

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        self._check_token(token)
        assert container_id == "synthetic-container-id"
        journal_paths = tuple(self._operation_directory.glob("*.json"))
        assert len(journal_paths) == 1
        journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "PUBLISH_REQUESTED"
        assert _WORKFLOW_TEXT not in journal_paths[0].read_text(encoding="utf-8")
        assert _TOKEN not in journal_paths[0].read_text(encoding="utf-8")
        self.mutation_calls.append("publish_container")
        return "synthetic-media-id"

    @staticmethod
    def _check_token(token: SecretStr) -> None:
        assert token.get_secret_value() == _TOKEN


class _FakeBrowserSession:
    def __init__(self) -> None:
        self.closed = False

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        assert url == "https://www.threads.com/"
        assert allowed_origins == frozenset({BROWSER_FEED_ORIGIN})

    async def collect_feed_permalinks(self, *, candidate_bound: int) -> tuple[str, ...]:
        assert candidate_bound > 0
        return (_FEED_REF,)

    async def scroll_feed(self) -> None:
        raise AssertionError("a single synthetic feed result needs no second page")

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserEngine:
    def __init__(self) -> None:
        self.sessions: list[_FakeBrowserSession] = []

    async def open(self, request: BrowserLaunchRequest) -> _FakeBrowserSession:
        assert request.headless is False
        assert request.profile_directory.is_dir()
        session = _FakeBrowserSession()
        self.sessions.append(session)
        return session


class _InterruptedTiming:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.elapsed = 0.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


def test_synthetic_cli_and_recurrence_e2e_use_only_local_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(cli_module.__file__).resolve()).casefold()

    root = tmp_path / "standalone-data"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_SPLIT07_TEST", _TOKEN)
    api = _SyntheticThreadsAPI(root)

    @asynccontextmanager
    async def fake_http_client(_settings: Settings) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(app_module, "build_threads_http_client", fake_http_client)
    monkeypatch.setattr(app_module, "HttpThreadsAPI", lambda _client: api)

    assert cli_module.main(["account", "add", "synthetic_account"]) == 0
    capsys.readouterr()
    assert (
        cli_module.main(
            [
                "account",
                "credential",
                "set-env",
                "synthetic_account",
                "THREADS_PLATFORM_THREADS_TOKEN_SPLIT07_TEST",
            ]
        )
        == 0
    )
    account_output = capsys.readouterr().out
    account_path = root / "accounts" / "synthetic_account.json"
    account_document = account_path.read_text(encoding="utf-8")
    assert "env://THREADS_PLATFORM_THREADS_TOKEN_SPLIT07_TEST" in account_document
    assert _TOKEN not in account_document
    assert _TOKEN not in account_output

    assert cli_module.main(["api", "quota", "synthetic_account"]) == 0
    api_output = capsys.readouterr().out
    assert api_output == "quota usage=2 total=100 reply_usage=1 reply_total=50\n"

    accounts = LocalAccountStore(root)
    account = accounts.get("synthetic_account")
    profile_path = root / "profiles" / str(account.id)
    profile_path.mkdir(parents=True)
    browser = _FakeBrowserEngine()
    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: browser)
    assert cli_module.main(["feed", "synthetic_account", "--limit", "1"]) == 0
    browser_output = capsys.readouterr().out
    assert _FEED_REF in browser_output
    assert len(browser.sessions) == 1 and browser.sessions[0].closed

    assert cli_module.main(["nurture", "run", "synthetic_account", "--preset", "recruitment"]) == 0
    nurture_output = capsys.readouterr().out
    assert "outcome=SUCCESS" in nurture_output and "decision=NO_ACTION" in nurture_output
    assert api.mutation_calls == []
    assert len(browser.sessions) == 1

    workflow_text = tmp_path / "synthetic-workflow.json"
    workflow_text.write_text(
        json.dumps(
            {
                "version": 1,
                "account": "synthetic_account",
                "steps": [
                    {"action": "quota"},
                    {"action": "post_text", "text": _WORKFLOW_TEXT},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert cli_module.main(["workflow", "run", str(workflow_text)]) == 0
    workflow_output = capsys.readouterr().out
    assert "workflow completed steps=2" in workflow_output
    assert _WORKFLOW_TEXT not in workflow_output
    assert api.mutation_calls == ["create_container", "publish_container"]

    operation_path = next((root / "operations").glob("*.json"))
    operation_document = json.loads(operation_path.read_text(encoding="utf-8"))
    assert operation_document["phase"] == "PUBLISHED"
    assert operation_document["media_id"] == "synthetic-media-id"
    assert _TOKEN not in operation_path.read_text(encoding="utf-8")
    assert _WORKFLOW_TEXT not in operation_path.read_text(encoding="utf-8")
    assert cli_module.main(["operation", "show", operation_document["id"]]) == 0
    operation_output = capsys.readouterr().out
    assert "phase=PUBLISHED" in operation_output
    assert _TOKEN not in operation_output and _WORKFLOW_TEXT not in operation_output

    recurrence_plan_path = tmp_path / "synthetic-read-only-workflow.json"
    recurrence_plan_path.write_text(
        json.dumps({"version": 1, "account": "synthetic_account", "steps": [{"action": "quota"}]}),
        encoding="utf-8",
    )
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    recurrence_store = LocalRecurrenceStore(root, accounts)
    recurrence = recurrence_store.create(recurrence_plan_path, 300, now=now)
    executed: list[WorkflowPlan] = []

    async def interrupt_after_one_read(plan: WorkflowPlan) -> None:
        assert is_read_only_workflow(plan)
        executed.append(plan)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            LocalRecurrenceRunner(recurrence_store, _InterruptedTiming(now)).run(
                recurrence.id,
                interrupt_after_one_read,
            )
        )

    assert len(executed) == 1
    assert recurrence_store.get(recurrence.id).last_outcome == "INTERRUPTED"
    assert api.mutation_calls == ["create_container", "publish_container"]
    runner_lock = recurrence_store.acquire_runner_lock(recurrence.id)
    runner_lock.release()

    combined_output = (
        account_output + api_output + browser_output + nurture_output + workflow_output
    )
    assert _TOKEN not in combined_output
    assert str(root) not in combined_output
