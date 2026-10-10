from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self, cast
from uuid import UUID

import httpx2
import pytest

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.accounts as account_module
import threads_platform.standalone.app as app_module
import threads_platform.standalone.runtime as runtime_module
from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    PublishingQuota,
    RemoteDiscoveryThread,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
)
from threads_platform.browser.capabilities import (
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.config import Settings
from threads_platform.standalone.__main__ import main
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.mutations import (
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)


def _set_data_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))


def _write_workflow(path: Path, steps: list[object], *, account: str = "alice") -> Path:
    path.write_text(
        json.dumps({"version": 1, "account": account, "steps": steps}),
        encoding="utf-8",
    )
    return path


class _FakeAsyncClientContext:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> Self:
        self.enter_count += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exit_count += 1


def _install_fake_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeAsyncClientContext:
    context = _FakeAsyncClientContext()

    def fake_builder(_settings: Settings) -> httpx2.AsyncClient:
        return cast(httpx2.AsyncClient, context)

    monkeypatch.setattr(
        app_module,
        "build_threads_http_client",
        fake_builder,
    )
    return context


def _assert_installed_wheel_when_requested() -> None:
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(cli_module.__file__).resolve()).casefold()


def test_cli_workflow_ambiguous_post_error_includes_step_and_operation_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    text_sentinel = "workflow-post-private-text"
    token_sentinel = "workflow-secret-token"
    workflow_path = _write_workflow(
        tmp_path / "private-workflow.json",
        [{"action": "quota"}, {"action": "post_text", "text": text_sentinel}],
    )
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        return PublishingQuota(1, 100)

    async def fail_publish(
        runtime: LocalThreadsMutationRuntime, alias: str, text: str
    ) -> PublishedTextResult:
        assert text == text_sentinel
        raise StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id)

    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_WORKFLOW_TEST", token_sentinel)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(app_module.LocalThreadsMutationRuntime, "publish_text", fail_publish)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == (f"ERROR PUBLISH_OUTCOME_AMBIGUOUS step=2 operation={operation_id}\n")
    assert str(workflow_path) not in captured.err
    assert text_sentinel not in captured.out + captured.err
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_failure_discards_buffered_output_and_closes_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "conversation", "thread_id": "thread-2"},
        ],
    )
    context = _install_fake_api_client(monkeypatch)
    calls: list[str] = []

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        calls.append("quota")
        return PublishingQuota(3, 100)

    async def fail_media(runtime: LocalThreadsApiRuntime, alias: str, media_id: str) -> RemoteMedia:
        calls.append("media")
        raise StandaloneApiError("INVALID_MEDIA_ID")

    async def fake_conversation(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append("conversation")
        return ReplyPage((), None, False)

    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "media", fail_media)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "conversation", fake_conversation)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_MEDIA_ID step=2\n"
    assert calls == ["quota", "media"]
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_formats_mixed_api_and_browser_reads_with_step_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("local-alice")
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [
            {"action": "public_profile", "username": "alice"},
            {
                "action": "search",
                "query": "private query sentinel",
                "mode": "KEYWORD",
                "type": "TOP",
                "limit": 25,
            },
            {"action": "feed", "limit": 3},
            {"action": "profile", "username": "alice"},
            {"action": "thread", "thread_ref": "/@alice/post/post-1"},
            {"action": "profile_posts", "username": "alice", "limit": 10},
            {"action": "mentions", "limit": 5},
        ],
        account="local-alice",
    )
    context = _install_fake_api_client(monkeypatch)
    events: list[tuple[object, ...]] = []

    async def fake_public_profile(
        runtime: LocalThreadsApiRuntime, alias: str, username: str
    ) -> RemotePublicProfile:
        events.append(("public_profile", alias, username))
        return RemotePublicProfile("author-1", username, "Alice", None, None)

    async def fake_search(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        events.append(("search", alias, query, search_mode, search_type, after, limit))
        return DiscoveryPage(
            (
                RemoteDiscoveryThread(
                    "search-thread",
                    "author-1",
                    "alice",
                    "bounded search text",
                    "https://www.threads.com/@alice/post/search-thread",
                    "TEXT",
                    None,
                    False,
                    True,
                ),
            ),
            None,
            False,
        )

    async def fake_profile_posts(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        username: str,
        *,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        events.append(("profile_posts", alias, username, after, limit))
        return DiscoveryPage((), None, False)

    async def fake_mentions(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        *,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        events.append(("mentions", alias, after, limit))
        return DiscoveryPage((), None, False)

    class FakeLocalRuntime:
        def __init__(self, _root: Path, _store: LocalAccountStore) -> None:
            pass

        async def browse_feed(self, alias: str, limit: int) -> BrowserFeedResultV1:
            events.append(("feed", alias, limit))
            return BrowserFeedResultV1(
                observations=(
                    BrowserFeedItemResultV1(
                        thread_ref="https://www.threads.com/@alice/post/feed-thread",
                        author_username="alice",
                        position=0,
                    ),
                ),
                truncated=False,
            )

        async def open_profile(self, alias: str, username: str) -> BrowserTargetOpenResultV1:
            events.append(("profile", alias, username))
            return BrowserTargetOpenResultV1(target_kind="PROFILE", target_ref=f"/@{username}")

        async def open_thread(self, alias: str, thread_ref: str) -> BrowserTargetOpenResultV1:
            events.append(("thread", alias, thread_ref))
            return BrowserTargetOpenResultV1(target_kind="THREAD", target_ref=thread_ref)

    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "public_profile", fake_public_profile)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "search", fake_search)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "profile_posts", fake_profile_posts)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "mentions", fake_mentions)
    monkeypatch.setattr(runtime_module, "LocalRuntime", FakeLocalRuntime)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out == (
        "workflow step=1 action=public_profile profile id=author-1 username=alice "
        "name=Alice bio=- picture=-\n"
        "workflow step=2 action=search count=1 has_more=false next_cursor=-\n"
        "workflow step=2 action=search thread search-thread username=alice timestamp=- "
        "media_type=TEXT quote=false has_replies=true "
        "permalink=https://www.threads.com/@alice/post/search-thread "
        "text=bounded search text\n"
        "workflow step=3 action=feed count=1 truncated=false\n"
        "workflow step=3 action=feed item position=0 "
        "thread_ref=https://www.threads.com/@alice/post/feed-thread author=alice text=-\n"
        "workflow step=4 action=profile recognized target=/@alice\n"
        "workflow step=5 action=thread recognized target=/@alice/post/post-1\n"
        "workflow step=6 action=profile_posts count=0 has_more=false next_cursor=-\n"
        "workflow step=7 action=mentions count=0 has_more=false next_cursor=-\n"
        "workflow completed steps=7\n"
    )
    assert "private query sentinel" not in captured.out
    assert events == [
        ("public_profile", "local-alice", "alice"),
        (
            "search",
            "local-alice",
            "private query sentinel",
            DiscoverySearchMode.KEYWORD,
            DiscoverySearchType.TOP,
            None,
            25,
        ),
        ("feed", "local-alice", 3),
        ("profile", "local-alice", "alice"),
        ("thread", "local-alice", "/@alice/post/post-1"),
        ("profile_posts", "local-alice", "alice", None, 10),
        ("mentions", "local-alice", None, 5),
    ]
    assert context.enter_count == context.exit_count == 1


def test_cli_workflow_invalid_plan_is_rejected_before_settings_or_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    workflow_path = _write_workflow(
        tmp_path / "private-workflow.json",
        [
            {"action": "quota"},
            {"action": "feed", "limit": 3},
            {"action": "thread", "thread_ref": "INVALID"},
        ],
    )
    calls: list[str] = []

    def fail_settings() -> Settings:
        calls.append("settings")
        raise AssertionError("invalid workflow must be rejected before Settings")

    def fail_client(_settings: Settings) -> httpx2.AsyncClient:
        calls.append("http")
        raise AssertionError("invalid workflow must be rejected before HTTP client")

    monkeypatch.setattr(app_module, "Settings", fail_settings)
    monkeypatch.setattr(app_module, "build_threads_http_client", fail_client)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_WORKFLOW\n"
    assert str(workflow_path) not in captured.out + captured.err
    assert calls == []


def test_cli_workflow_missing_account_is_rejected_before_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    workflow_path = _write_workflow(tmp_path / "workflow.json", [{"action": "quota"}])
    calls: list[str] = []

    def fail_client(_settings: Settings) -> httpx2.AsyncClient:
        calls.append("http")
        raise AssertionError("missing local account must fail before HTTP client")

    monkeypatch.setattr(app_module, "build_threads_http_client", fail_client)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_NOT_FOUND\n"
    assert calls == []


def test_cli_workflow_mixed_reads_buffer_exact_output_and_uses_one_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_installed_wheel_when_requested()
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "replies", "thread_id": "thread-1", "after": "cursor-in"},
            {"action": "conversation", "thread_id": "thread-2"},
        ],
    )
    context = _install_fake_api_client(monkeypatch)
    calls: list[tuple[object, ...]] = []

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        calls.append(("quota", alias))
        return PublishingQuota(3, 100, None, 20)

    async def fake_media(runtime: LocalThreadsApiRuntime, alias: str, media_id: str) -> RemoteMedia:
        calls.append(("media", alias, media_id))
        return RemoteMedia(
            "media-1", "  public\n media text  ", "https://www.threads.com/t/1", "then"
        )

    async def fake_replies(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append(("replies", alias, thread_id, after))
        return ReplyPage(
            (RemoteReply("reply-1", "  reply\n text ", None, "root-1", None),), "cursor-out", True
        )

    async def fake_conversation(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append(("conversation", alias, thread_id, after))
        return ReplyPage((), None, False)

    def fail_if_browser_login(*args: object, **kwargs: object) -> None:
        raise AssertionError("workflow commands must not invoke browser login")

    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "media", fake_media)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "replies", fake_replies)
    monkeypatch.setattr(app_module.LocalThreadsApiRuntime, "conversation", fake_conversation)
    monkeypatch.setattr(runtime_module, "LocalRuntime", fail_if_browser_login)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        "workflow step=1 action=quota usage=3 total=100 reply_usage=- reply_total=20\n"
        "workflow step=2 action=media id=media-1 timestamp=then "
        "permalink=https://www.threads.com/t/1 text=public media text\n"
        "workflow step=3 action=replies count=1 has_more=true next_cursor=cursor-out\n"
        "workflow step=3 action=replies reply reply-1 timestamp=- root=root-1 "
        "parent=- text=reply text\n"
        "workflow step=4 action=conversation count=0 has_more=false next_cursor=-\n"
        "workflow completed steps=4\n"
    )
    assert captured.err == ""
    assert calls == [
        ("quota", "alice"),
        ("media", "alice", "media-1"),
        ("replies", "alice", "thread-1", "cursor-in"),
        ("conversation", "alice", "thread-2", None),
    ]
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_post_success_is_exact_and_never_echoes_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_installed_wheel_when_requested()
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    text_sentinel = "chosen workflow post text do not echo"
    token_sentinel = "workflow-token-never-output"
    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_WORKFLOW_TEST", token_sentinel)
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [{"action": "post_text", "text": text_sentinel}],
    )
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    calls: list[tuple[str, str]] = []

    async def fake_publish(
        runtime: LocalThreadsMutationRuntime, alias: str, text: str
    ) -> PublishedTextResult:
        calls.append((alias, text))
        return PublishedTextResult(operation_id, "published-123")

    monkeypatch.setattr(app_module.LocalThreadsMutationRuntime, "publish_text", fake_publish)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", text_sentinel)]
    assert captured.out == (
        f"workflow step=1 action=post_text published operation={operation_id} "
        "media=published-123\nworkflow completed steps=1\n"
    )
    assert captured.err == ""
    assert text_sentinel not in captured.out + captured.err
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1
