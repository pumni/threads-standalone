from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

import pytest

from threads_platform.api.contracts import (
    DiscoveryPage,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
)
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import (
    AccountNurtureInboundConfig,
    NurturePresetV1,
    build_account_nurture_inbound_config,
    get_nurture_preset,
)
from threads_platform.standalone.nurture_conversations import (
    NurtureInboundStatus,
    collect_nurture_inbound,
)
from threads_platform.standalone.nurture_store import fingerprint_remote_reply

_NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
_OWNER = "owner_public_sentinel"
_REPLY_TEXT = "raw_reply_text_sentinel"
_IDENTITY_DEFAULT = object()


def _preset(
    *,
    owner: str | None = _OWNER,
    maximum_threads: int = 3,
    per_source_limit: int = 5,
) -> NurturePresetV1:
    return replace(
        get_nurture_preset("recruitment"),
        owner_public_username=owner,
        max_owned_threads_inspected_per_explicit_run=maximum_threads if owner else 0,
        per_source_page_limit=per_source_limit,
    )


def _binding(
    preset: NurturePresetV1 | None = None,
    *,
    alias: str = "alice",
    owner: str = _OWNER,
    maximum_threads: int | None = None,
) -> AccountNurtureInboundConfig:
    active_preset = preset or _preset()
    return build_account_nurture_inbound_config(
        active_preset,
        alias,
        owner,
        (
            active_preset.max_owned_threads_inspected_per_explicit_run
            if maximum_threads is None
            else maximum_threads
        ),
    )


def _thread(thread_id: str) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(thread_id, None, None, None, None, None, None, None, None)


def _reply(
    reply_id: str,
    *,
    owned: bool | None = False,
    root: str | None = "root-a",
    parent: str | None = "root-a",
    timestamp: str | None = "2026-10-07T01:00:00+00:00",
) -> RemoteReply:
    return RemoteReply(reply_id, _REPLY_TEXT, timestamp, root, parent, owned)


def test_reply_fingerprint_is_stable_and_namespaced() -> None:
    assert fingerprint_remote_reply("reply-1") == sha256(b"reply:reply-1").hexdigest()
    assert fingerprint_remote_reply("reply-1") == fingerprint_remote_reply("reply-1")


class _Api:
    def __init__(
        self,
        profile_threads: tuple[RemoteDiscoveryThread, ...],
        pages: dict[str, ReplyPage] | None = None,
        *,
        profile_failure: Exception | None = None,
        conversation_failure: Exception | None = None,
        identity: object = _IDENTITY_DEFAULT,
        identity_failure: Exception | None = None,
    ) -> None:
        self.profile_threads = profile_threads
        self.pages = pages or {}
        self.profile_failure = profile_failure
        self.conversation_failure = conversation_failure
        self.identity = (
            RemoteAuthenticatedProfile("synthetic_authenticated_id", _OWNER)
            if identity is _IDENTITY_DEFAULT
            else identity
        )
        self.identity_failure = identity_failure
        self.identity_calls: list[str] = []
        self.profile_calls: list[tuple[str, str, str | None, int]] = []
        self.conversation_calls: list[tuple[str, str, str | None]] = []

    async def authenticated_profile(self, alias: str) -> RemoteAuthenticatedProfile:
        self.identity_calls.append(alias)
        if self.identity_failure is not None:
            raise self.identity_failure
        return cast(RemoteAuthenticatedProfile, self.identity)

    async def profile_posts(
        self, alias: str, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        self.profile_calls.append((alias, username, after, limit))
        if self.profile_failure is not None:
            raise self.profile_failure
        return DiscoveryPage(self.profile_threads, "profile-cursor-sentinel", True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.conversation_calls.append((alias, thread_id, after))
        if self.conversation_failure is not None:
            raise self.conversation_failure
        return self.pages.get(thread_id, ReplyPage((), "conversation-cursor-sentinel", True))


@pytest.mark.asyncio
async def test_unconfigured_owner_skips_profile_and_conversation_calls() -> None:
    api = _Api(())

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", _preset(owner=None)
    )

    assert result.discovered_count == 0
    assert result.deduped_count == 0
    assert result.candidates == ()
    assert result.status is NurtureInboundStatus.NOT_CONFIGURED
    assert api.identity_calls == []
    assert api.profile_calls == []
    assert api.conversation_calls == []


@pytest.mark.asyncio
async def test_owner_scan_uses_one_bounded_profile_page_and_unique_thread_prefix() -> None:
    api = _Api(
        (_thread("own-a"), _thread("own-a"), _thread("own-b"), _thread("own-c")),
        {
            "own-a": ReplyPage((), "reply-cursor-a", True),
            "own-b": ReplyPage((), "reply-cursor-b", True),
        },
    )
    preset = _preset(maximum_threads=2, per_source_limit=5)

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", preset, _binding(preset)
    )

    assert result.candidates == ()
    assert result.status is NurtureInboundStatus.READY
    assert api.identity_calls == ["alice"]
    assert api.profile_calls == [("alice", _OWNER, None, 2)]
    assert api.conversation_calls == [("alice", "own-a", None), ("alice", "own-b", None)]


@pytest.mark.asyncio
async def test_profile_request_limit_uses_per_source_cap_when_it_is_smaller() -> None:
    api = _Api((_thread("own-a"), _thread("own-b")))
    preset = _preset(maximum_threads=5, per_source_limit=2)

    await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api),
        "alice",
        preset,
        _binding(preset),
    )

    assert api.profile_calls == [("alice", _OWNER, None, 2)]
    assert api.conversation_calls == [("alice", "own-a", None), ("alice", "own-b", None)]


@pytest.mark.asyncio
async def test_only_false_ownership_with_exact_root_is_proven_and_replies_dedupe() -> None:
    duplicate = _reply("reply-good", root="root-a", parent="reply-parent")
    api = _Api(
        (_thread("root-a"), _thread("root-b")),
        {
            "root-a": ReplyPage(
                (
                    _reply("reply-own", owned=True),
                    _reply("reply-unknown", owned=None),
                    _reply("reply-wrong-root", root="other-root"),
                    _reply("reply-missing-root", root=None),
                    duplicate,
                    _reply(
                        "reply-good",
                        root="root-a",
                        parent="reply-parent",
                        timestamp="2030-10-07T01:00:00+00:00",
                    ),
                ),
                "cursor-a",
                True,
            ),
            "root-b": ReplyPage(
                (
                    duplicate,
                    _reply("reply-good-b", root="root-b", parent="nested-parent"),
                ),
                "cursor-b",
                True,
            ),
        },
    )

    preset = _preset(maximum_threads=2, per_source_limit=10)
    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", preset, _binding(preset)
    )

    assert result.status is NurtureInboundStatus.READY
    assert result.discovered_count == 8
    assert result.deduped_count == 6
    assert {candidate.fingerprint for candidate in result.candidates} == {
        fingerprint_remote_reply("reply-good"),
        fingerprint_remote_reply("reply-good-b"),
    }
    inbound = next(
        candidate
        for candidate in result.candidates
        if candidate.fingerprint == fingerprint_remote_reply("reply-good")
    )
    assert inbound.timestamp == datetime.fromisoformat("2026-10-07T01:00:00+00:00")
    assert all(_REPLY_TEXT not in repr(candidate) for candidate in result.candidates)
    assert api.conversation_calls == [("alice", "root-a", None), ("alice", "root-b", None)]


@pytest.mark.asyncio
async def test_inbound_order_is_newest_first_null_last_then_fingerprint() -> None:
    replies = (
        _reply("reply-null", timestamp=None),
        _reply("reply-old", timestamp="2026-10-06T01:00:00+00:00"),
        _reply("reply-tie-b", timestamp="2026-10-07T00:00:00+00:00"),
        _reply("reply-new", timestamp="2026-10-07T01:00:00+00:00"),
        _reply("reply-tie-a", timestamp="2026-10-07T00:00:00+00:00"),
        _reply("reply-invalid-time", timestamp="not-a-timestamp"),
    )
    api = _Api((_thread("root-a"),), {"root-a": ReplyPage(replies, None, False)})

    preset = _preset(maximum_threads=1, per_source_limit=10)
    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", preset, _binding(preset)
    )

    fingerprints = [candidate.fingerprint for candidate in result.candidates]
    assert fingerprints == [
        fingerprint_remote_reply("reply-new"),
        *sorted(
            (
                fingerprint_remote_reply("reply-tie-a"),
                fingerprint_remote_reply("reply-tie-b"),
            )
        ),
        fingerprint_remote_reply("reply-old"),
        *sorted(
            (
                fingerprint_remote_reply("reply-invalid-time"),
                fingerprint_remote_reply("reply-null"),
            )
        ),
    ]


@pytest.mark.asyncio
async def test_owner_scan_error_has_distinct_safe_not_ready_status() -> None:
    api = _Api((), profile_failure=RuntimeError("owner username and reply text sentinel"))

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", _preset(), _binding()
    )

    assert result.status is NurtureInboundStatus.READ_FAILED
    assert result.candidates == ()
    assert "owner username" not in repr(result)
    assert "reply text" not in repr(result)
    assert api.identity_calls == ["alice"]
    assert api.profile_calls == [("alice", _OWNER, None, 3)]
    assert api.conversation_calls == []


@pytest.mark.asyncio
async def test_mismatched_authenticated_username_never_reads_or_surfaces_inbound() -> None:
    api = _Api(
        (_thread("root-a"),),
        {"root-a": ReplyPage((_reply("reply-hidden"),), None, False)},
        identity=RemoteAuthenticatedProfile("synthetic_other_id", "other_synthetic_owner"),
    )

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", _preset(), _binding()
    )

    assert result.status is NurtureInboundStatus.OWNERSHIP_MISMATCH
    assert result.candidates == ()
    assert api.identity_calls == ["alice"]
    assert api.profile_calls == []
    assert api.conversation_calls == []


@pytest.mark.asyncio
async def test_config_bound_to_another_alias_never_resolves_identity_or_reads_posts() -> None:
    api = _Api((_thread("root-a"),))
    binding = _binding(alias="bob")

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", _preset(), binding
    )

    assert result.status is NurtureInboundStatus.ACCOUNT_MISMATCH
    assert result.candidates == ()
    assert api.identity_calls == []
    assert api.profile_calls == []
    assert api.conversation_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "identity_failure", "expected"),
    [
        (None, None, NurtureInboundStatus.OWNERSHIP_UNVERIFIED),
        (
            None,
            ThreadsAPIError("THREADS_PERMISSION_DENIED"),
            NurtureInboundStatus.PERMISSION_MISSING,
        ),
        (
            None,
            RuntimeError("synthetic identity response failure"),
            NurtureInboundStatus.OWNERSHIP_UNVERIFIED,
        ),
    ],
)
async def test_missing_or_failed_identity_evidence_fails_closed(
    identity: object,
    identity_failure: Exception | None,
    expected: NurtureInboundStatus,
) -> None:
    api = _Api(
        (_thread("root-a"),),
        identity=identity,
        identity_failure=identity_failure,
    )

    result = await collect_nurture_inbound(
        cast(LocalThreadsApiRuntime, api), "alice", _preset(), _binding()
    )

    assert result.status is expected
    assert result.candidates == ()
    assert api.identity_calls == ["alice"]
    assert api.profile_calls == []
    assert api.conversation_calls == []
