from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from threads_platform.api.contracts import (
    DiscoveryPage,
    PublishingQuota,
    RemoteDiscoveryThread,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
    ThreadPostInsightMetric,
    ThreadPostInsightName,
    ThreadPostInsights,
)
from threads_platform.standalone.__main__ import main


def test_help_is_available_without_runtime_capabilities(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "usage: threads-local" in capsys.readouterr().out


def test_help_lists_every_account_and_api_read_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["api", "--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for command in (
        "quota",
        "media",
        "post-insights",
        "public-profile",
        "profile-posts",
        "search",
        "mentions",
        "replies",
        "conversation",
    ):
        assert command in output
    assert "publish" not in output


def test_help_lists_local_browser_commands_without_mutation_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for command in ("account", "profile", "thread", "feed", "api"):
        assert command in output
    for forbidden in ("publish", "reply", "worker"):
        assert forbidden not in output


def test_browser_cli_shapes_match_local_contract() -> None:
    import threads_platform.standalone.__main__ as cli

    profile = cli._build_parser().parse_args(["profile", "synthetic_account", "synthetic_user"])
    thread = cli._build_parser().parse_args(
        ["thread", "synthetic_account", "/@synthetic/post/fake_1"]
    )
    feed = cli._build_parser().parse_args(["feed", "synthetic_account", "--limit", "7"])
    login = cli._build_parser().parse_args(["account", "login", "synthetic_account"])

    assert (profile.alias, profile.username) == ("synthetic_account", "synthetic_user")
    assert (thread.alias, thread.thread_ref) == (
        "synthetic_account",
        "/@synthetic/post/fake_1",
    )
    assert (feed.alias, feed.limit) == ("synthetic_account", 7)
    assert (login.account_command, login.alias) == ("login", "synthetic_account")
    assert cli._build_parser().parse_args(["feed", "synthetic_account"]).limit == 10


def test_account_add_list_and_set_env_keep_only_a_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))

    assert main(["account", "add", "synthetic_account"]) == 0
    added = capsys.readouterr().out.strip().split()
    assert added[:2] == ["added", "synthetic_account"]
    UUID(added[2])

    assert (
        main(
            [
                "account",
                "credential",
                "set-env",
                "synthetic_account",
                "THREADS_PLATFORM_THREADS_TOKEN_SPLIT02_TEST",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "credential configured synthetic_account\n"

    assert main(["account", "list"]) == 0
    listed = capsys.readouterr().out
    assert listed == f"synthetic_account {added[2]}\n"
    account_file = tmp_path / "accounts" / "synthetic_account.json"
    account_data = account_file.read_text(encoding="utf-8")
    assert '"credential_ref": "env://THREADS_PLATFORM_THREADS_TOKEN_SPLIT02_TEST"' in account_data
    assert "SYNTHETIC_TOKEN" not in account_data


class _CliRuntime:
    async def quota(self, alias: str) -> PublishingQuota:
        assert alias == "synthetic_account"
        return PublishingQuota(usage=3, total=250, reply_usage=1, reply_total=100)

    async def media(self, alias: str, media_id: str) -> RemoteMedia:
        assert (alias, media_id) == ("synthetic_account", "synthetic_media")
        return RemoteMedia("synthetic_media", "synthetic text", None, "synthetic timestamp")

    async def post_insights(self, alias: str, media_id: str) -> ThreadPostInsights:
        assert (alias, media_id) == ("synthetic_account", "synthetic_media")
        metrics = tuple(ThreadPostInsightMetric(name, 1) for name in ThreadPostInsightName)
        return ThreadPostInsights("synthetic_media", "lifetime", metrics)

    async def public_profile(self, alias: str, username: str) -> RemotePublicProfile:
        assert (alias, username) == ("synthetic_account", "synthetic_user")
        return RemotePublicProfile("synthetic_author", username, None, None, None)

    async def profile_posts(
        self, alias: str, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        assert (alias, username, after, limit) == (
            "synthetic_account",
            "synthetic_user",
            "synthetic_cursor",
            7,
        )
        return _discovery_page()

    async def search(
        self,
        alias: str,
        query: str,
        *,
        search_mode: object,
        search_type: object,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert (alias, query, after, limit) == (
            "synthetic_account",
            "synthetic query",
            "synthetic_cursor",
            7,
        )
        assert getattr(search_mode, "value", None) == "KEYWORD"
        assert getattr(search_type, "value", None) == "RECENT"
        return _discovery_page()

    async def mentions(self, alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        assert (alias, after, limit) == ("synthetic_account", "synthetic_cursor", 7)
        return _discovery_page()

    async def replies(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        assert (alias, thread_id, after) == (
            "synthetic_account",
            "synthetic_thread",
            "synthetic_cursor",
        )
        return _reply_page()

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        return await self.replies(alias, thread_id, after=after)


def _discovery_page() -> DiscoveryPage:
    item = RemoteDiscoveryThread(
        "synthetic_thread",
        None,
        "synthetic_user",
        "synthetic text",
        None,
        "TEXT",
        datetime(2026, 1, 1, tzinfo=UTC),
        False,
        True,
    )
    return DiscoveryPage((item,), "synthetic_cursor", True)


def _reply_page() -> ReplyPage:
    reply = RemoteReply("synthetic_reply", "synthetic text", None, None, None)
    return ReplyPage((reply,), "synthetic_cursor", True)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["api", "quota", "synthetic_account"], "quota usage=3 total=250"),
        (["api", "media", "synthetic_account", "synthetic_media"], "media synthetic_media"),
        (
            ["api", "post-insights", "synthetic_account", "synthetic_media"],
            "post-insights period=lifetime",
        ),
        (
            ["api", "public-profile", "synthetic_account", "synthetic_user"],
            "profile id=synthetic_author",
        ),
        (
            [
                "api",
                "profile-posts",
                "synthetic_account",
                "synthetic_user",
                "--after",
                "synthetic_cursor",
                "--limit",
                "7",
            ],
            "profile-posts count=1",
        ),
        (
            [
                "api",
                "search",
                "synthetic_account",
                "synthetic query",
                "--mode",
                "keyword",
                "--type",
                "recent",
                "--after",
                "synthetic_cursor",
                "--limit",
                "7",
            ],
            "search count=1",
        ),
        (
            ["api", "mentions", "synthetic_account", "--after", "synthetic_cursor", "--limit", "7"],
            "mentions count=1",
        ),
        (
            [
                "api",
                "replies",
                "synthetic_account",
                "synthetic_thread",
                "--after",
                "synthetic_cursor",
            ],
            "replies count=1",
        ),
        (
            [
                "api",
                "conversation",
                "synthetic_account",
                "synthetic_thread",
                "--after",
                "synthetic_cursor",
            ],
            "conversation count=1",
        ),
    ],
)
def test_api_read_cli_commands_use_the_runtime(
    arguments: list[str],
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))

    @asynccontextmanager
    async def fake_app(root: Path, accounts: object = None):
        del root, accounts
        yield SimpleNamespace(api=_CliRuntime())

    monkeypatch.setattr(cli, "build_standalone_app", fake_app)
    assert main(arguments) == 0
    assert expected in capsys.readouterr().out
