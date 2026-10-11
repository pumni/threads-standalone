from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import threads_platform.standalone.recruitment_session as session_module
from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    PublishingQuota,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    ReplyPage,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.nurture_content import ContentCandidateV1, ContentCategory
from threads_platform.standalone.nurture_draft import NurtureDraft
from threads_platform.standalone.nurture_store import (
    NurtureStore,
    fingerprint_remote_thread,
)

_REMOTE_TEXT = "REMOTE_SYNTHETIC_TARGET_TEXT career question"
_POST_TEXT = "PRIVATE_SYNTHETIC_POST_TEXT"
_REPLY_TEXT = "PRIVATE_SYNTHETIC_REPLY_TEXT"


def _thread() -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        "synthetic_thread_001",
        "synthetic_author",
        None,
        _REMOTE_TEXT,
        None,
        None,
        datetime.now(UTC),
        None,
        False,
    )


def _candidate() -> ContentCandidateV1:
    return ContentCandidateV1(
        candidate_id=uuid4(),
        account_alias="alice",
        preset_id="recruitment",
        source_id="synthetic_source_001",
        category=cast(ContentCategory, "CAREER_TIP"),
        text=_POST_TEXT,
    )


def _write_candidate_packet(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "POST_TEXT",
                "candidate_id": str(uuid4()),
                "account": "alice",
                "preset": "recruitment",
                "source_kind": "OPERATOR_SOURCE_ID",
                "source_id": "synthetic_source_packet_001",
                "category": "CAREER_TIP",
                "text": _POST_TEXT,
            }
        ),
        encoding="utf-8",
    )


def _write_reply_packet(path: Path, target_fingerprint: str, text: str) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "REPLY",
                "target_fingerprint": target_fingerprint,
                "text": text,
            }
        ),
        encoding="utf-8",
    )


class _FakeApiError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("PRIVATE_SYNTHETIC_API_ERROR")


class _FakeApi:
    def __init__(
        self,
        *,
        identity: str = "configured_owner",
        identity_error: Exception | None = None,
        profile_error: Exception | None = None,
        conversation_error: Exception | None = None,
        owned_posts: tuple[RemoteDiscoveryThread, ...] = (),
        search_entered: asyncio.Event | None = None,
        search_release: asyncio.Event | None = None,
    ) -> None:
        self.thread = _thread()
        self.identity = identity
        self.identity_error = identity_error
        self.profile_error = profile_error
        self.conversation_error = conversation_error
        self.owned_posts = owned_posts
        self.search_entered = search_entered
        self.search_release = search_release
        self.calls: list[tuple[object, ...]] = []

    async def search(
        self,
        alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("search", alias, query, search_mode, search_type, after, limit))
        if self.search_entered is not None:
            self.search_entered.set()
        if self.search_release is not None:
            await self.search_release.wait()
        return DiscoveryPage((self.thread,), "synthetic_cursor", True)

    async def mentions(self, alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        self.calls.append(("mentions", alias, after, limit))
        return DiscoveryPage((), None, True)

    async def profile_posts(
        self,
        alias: str,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("profile_posts", alias, username, after, limit))
        if self.profile_error is not None:
            raise self.profile_error
        return DiscoveryPage(self.owned_posts, None, True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.calls.append(("conversation", alias, thread_id, after))
        if self.conversation_error is not None:
            raise self.conversation_error
        return ReplyPage((), None, True)

    async def authenticated_profile(self, alias: str) -> RemoteAuthenticatedProfile:
        self.calls.append(("authenticated_profile", alias))
        if self.identity_error is not None:
            raise self.identity_error
        return RemoteAuthenticatedProfile("synthetic_owner_id", self.identity)

    async def quota(self, alias: str) -> PublishingQuota:
        self.calls.append(("quota", alias))
        return PublishingQuota(usage=0, total=100)


class _FakeMutations:
    def __init__(self, *, reply_failure: Exception | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.reply_failure = reply_failure

    async def create_reply(
        self,
        alias: str,
        thread_id: str,
        text: str,
        *,
        parent_reply_id: str | None = None,
    ) -> CreatedReplyResult:
        self.calls.append(("reply", alias, thread_id, text, parent_reply_id))
        if self.reply_failure is not None:
            raise self.reply_failure
        return CreatedReplyResult(uuid4(), "synthetic_reply_id")

    async def publish_text(self, alias: str, text: str) -> PublishedTextResult:
        self.calls.append(("post", alias, text))
        return PublishedTextResult(uuid4(), "synthetic_media_id")


def _install_fake_app(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeApi,
    mutations: _FakeMutations,
) -> list[bool]:
    mutation_modes: list[bool] = []

    @asynccontextmanager
    async def fake_builder(
        _root: Path,
        accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = False,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api is True
        assert include_browser is False
        mutation_modes.append(include_mutations)
        yield StandaloneAppContext(
            accounts,
            cast(LocalThreadsApiRuntime, api),
            cast(LocalThreadsMutationRuntime | None, mutations if include_mutations else None),
        )

    monkeypatch.setattr(session_module, "build_standalone_app", fake_builder)
    return mutation_modes


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _saved_receipts(root: Path, account_id: object) -> list[dict[str, object]]:
    directory = root / "nurture" / "runs" / str(account_id)
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in directory.rglob("*.json")
        if path.is_file()
    ]


class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _TtyOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _install_private_terminal(
    monkeypatch: pytest.MonkeyPatch,
    response: str = "",
) -> _TtyOutput:
    output = _TtyOutput()
    monkeypatch.setattr(session_module.sys, "stdin", _TtyInput(response))
    monkeypatch.setattr(session_module.sys, "stderr", output)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return output


def test_validate_is_zero_write_and_does_not_build_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    before = _snapshot(tmp_path)

    def forbidden_builder(*_args: object, **_kwargs: object) -> None:
        pytest.fail("validate must not construct a client or runtime")

    monkeypatch.setattr(session_module, "build_standalone_app", forbidden_builder)
    result = session_module.RecruitmentSession(tmp_path, accounts).validate("alice")

    assert result.mode is session_module.RecruitmentSessionMode.VALIDATE
    assert result.status == "READY"
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "nurture").exists()


def test_script_validate_is_zero_write_with_only_synthetic_environment(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    LocalAccountStore(data_root).add("alice")
    before = _snapshot(data_root)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT")
        if key in os.environ
    }
    environment["THREADS_LOCAL_DATA_ROOT"] = str(data_root)
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_recruitment.py"

    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env={key: value for key, value in environment.items() if key != "THREADS_LOCAL_DATA_ROOT"},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0
    assert "--inbound-owner-username" in help_result.stdout
    assert help_result.stderr == ""

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--account",
            "alice",
            "--validate",
            "--inbound-owner-username",
            "synthetic_owner",
            "--inbound-thread-cap",
            "1",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0
    assert completed.stdout == "recruitment-session mode=validate status=READY\n"
    assert completed.stderr == ""
    assert _snapshot(data_root) == before

    missing_root = tmp_path / "missing-data-root"
    missing_environment = dict(environment)
    missing_environment["THREADS_LOCAL_DATA_ROOT"] = str(missing_root)
    missing = subprocess.run(
        [sys.executable, str(script), "--account", "alice", "--validate"],
        cwd=tmp_path,
        env=missing_environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert missing.stderr == "ERROR LOCAL_DATA_ROOT_NOT_FOUND\n"
    assert not missing_root.exists()


def test_validate_missing_or_unwritable_root_fails_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing"
    missing_session = session_module.RecruitmentSession(missing_root)
    with pytest.raises(session_module.RecruitmentSessionError) as missing:
        missing_session.validate("alice")
    assert missing.value.code == "LOCAL_DATA_ROOT_NOT_FOUND"
    assert not missing_root.exists()

    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setattr(session_module.os, "access", lambda *_args: False)
    with pytest.raises(session_module.RecruitmentSessionError) as unavailable:
        session_module.RecruitmentSession(tmp_path, accounts).validate("alice")
    assert unavailable.value.code == "LOCAL_DATA_ROOT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_observe_uses_read_only_api_runtime_and_keeps_no_config_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    result = await session_module.RecruitmentSession(tmp_path, accounts).observe("alice")

    assert result.receipt is not None
    assert result.receipt.outcome == "SUCCESS"
    assert result.target_preview is not None
    assert mutation_modes == [False]
    assert mutations.calls == []
    assert any(call[0] == "search" for call in api.calls)
    assert not any(call[0] == "authenticated_profile" for call in api.calls)
    assert result.inbound_status == "INBOUND_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_review_returns_redacted_target_and_independent_own_content_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    result = await session_module.RecruitmentSession(tmp_path, accounts).review(
        "alice", content_candidate=_candidate()
    )

    assert result.receipt is not None
    assert result.target_preview is not None
    assert result.target_preview.target_text == _REMOTE_TEXT
    assert result.content_preview is not None
    assert result.content_preview.due is True
    assert result.content_preview.text == _POST_TEXT
    assert mutation_modes == [False]
    assert mutations.calls == []
    assert _REMOTE_TEXT not in repr(result)
    assert _POST_TEXT not in repr(result)
    assert _REMOTE_TEXT not in session_module._format_result(result)
    assert _POST_TEXT not in session_module._format_result(result)
    assert result.receipt.decision_codes == (
        "OBSERVE_ONLY",
        "CONTENT_RECOMMENDED_NO_APPLY",
    )


def test_cli_post_review_precedes_apply_on_private_terminal_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    packet = tmp_path / "synthetic-candidate.json"
    _write_candidate_packet(packet)
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)
    private_terminal = _install_private_terminal(monkeypatch, "APPLY\n")

    review_status = session_module.main(
        ["--account", "alice", "--review", "--review-post-file", str(packet)]
    )
    review_output = capsys.readouterr()
    assert review_status == 0
    assert "mode=review" in review_output.out
    assert "own_content=due" in review_output.out
    assert _POST_TEXT not in review_output.out
    assert _REMOTE_TEXT not in review_output.out
    assert "action: POST_TEXT" in private_terminal.getvalue()
    assert "  source: " in private_terminal.getvalue()
    assert 'source id: "synthetic_source_packet_001"' in private_terminal.getvalue()
    assert f"proposed post: {json.dumps(_POST_TEXT)}" in private_terminal.getvalue()
    assert mutation_modes == [False]
    assert mutations.calls == []

    apply_status = session_module.main(["--account", "alice", "--apply-post-file", str(packet)])
    apply_output = capsys.readouterr()
    assert apply_status == 0
    assert "mode=apply_post" in apply_output.out
    assert _POST_TEXT not in apply_output.out
    assert _REMOTE_TEXT not in apply_output.out
    assert mutation_modes == [False, False, True]
    assert [call[0] for call in mutations.calls] == ["post"]
    assert _POST_TEXT in private_terminal.getvalue()

    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert receipts
    assert all(_POST_TEXT not in json.dumps(receipt) for receipt in receipts)
    assert all(_REMOTE_TEXT not in json.dumps(receipt) for receipt in receipts)


def test_cli_reply_review_binds_exact_target_and_precedes_approved_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)
    fingerprint = fingerprint_remote_thread(api.thread.remote_thread_id)
    packet = tmp_path / "synthetic-reply.json"
    _write_reply_packet(packet, fingerprint, _REPLY_TEXT)
    private_terminal = _install_private_terminal(monkeypatch, "APPLY\n")

    review_status = session_module.main(
        ["--account", "alice", "--review", "--review-reply-file", str(packet)]
    )
    review_output = capsys.readouterr()
    assert review_status == 0
    assert "mode=review" in review_output.out
    assert _REPLY_TEXT not in review_output.out
    assert _REMOTE_TEXT not in review_output.out
    assert f"fingerprint: {fingerprint}" in private_terminal.getvalue()
    assert "  source: " in private_terminal.getvalue()
    assert "action: REPLY" in private_terminal.getvalue()
    assert f"proposed reply: {json.dumps(_REPLY_TEXT)}" in private_terminal.getvalue()
    assert mutation_modes == [False]
    assert mutations.calls == []

    apply_status = session_module.main(["--account", "alice", "--apply-reply-file", str(packet)])
    apply_output = capsys.readouterr()
    assert apply_status == 0
    assert "mode=apply_reply" in apply_output.out
    assert _REPLY_TEXT not in apply_output.out
    assert _REMOTE_TEXT not in apply_output.out
    assert mutation_modes == [False, False, True]
    assert [call[0] for call in mutations.calls] == ["reply"]
    assert mutations.calls[0][2] == api.thread.remote_thread_id
    assert mutations.calls[0][3] == _REPLY_TEXT
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert receipts
    assert all(_REPLY_TEXT not in json.dumps(receipt) for receipt in receipts)
    assert all(_REMOTE_TEXT not in json.dumps(receipt) for receipt in receipts)


def test_stale_reviewed_reply_fails_without_switching_to_another_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    api = _FakeApi()
    mutations = _FakeMutations()
    _install_fake_app(monkeypatch, api, mutations)
    packet = tmp_path / "stale-synthetic-reply.json"
    _write_reply_packet(packet, "0" * 64, _REPLY_TEXT)
    private_terminal = _install_private_terminal(monkeypatch, "APPLY\n")

    status = session_module.main(["--account", "alice", "--apply-reply-file", str(packet)])
    output = capsys.readouterr()

    assert status == 1
    assert output.out == ""
    assert output.err == ""  # Error goes only to the explicit local terminal stream.
    assert private_terminal.getvalue() == "ERROR NURTURE_DRAFT_TARGET_MISMATCH\n"
    assert mutations.calls == []


def test_script_inbound_configuration_binds_owner_and_honors_ephemeral_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    owned_post = RemoteDiscoveryThread(
        "synthetic_owned_thread_001",
        "synthetic_owner_id",
        "configured_owner",
        "PRIVATE_SYNTHETIC_OWN_POST_TEXT",
        None,
        None,
        datetime.now(UTC),
        False,
        True,
    )
    api = _FakeApi(owned_posts=(owned_post,))
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    status = session_module.main(
        [
            "--account",
            "alice",
            "--observe",
            "--inbound-owner-username",
            "configured_owner",
            "--inbound-thread-cap",
            "1",
        ]
    )

    assert status == 0
    assert [call[0] for call in api.calls] == [
        "authenticated_profile",
        "profile_posts",
        "conversation",
        "search",
        "mentions",
    ]
    assert api.calls[1][-1] == 1
    assert mutations.calls == []
    assert mutation_modes == [False]
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "SUCCESS"
    assert "configured_owner" not in json.dumps(receipts[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "failure_code", "expected_receipt_code", "expected_calls"),
    [
        (
            "identity",
            "THREADS_PERMISSION_DENIED",
            "INBOUND_PERMISSION_MISSING",
            ("authenticated_profile",),
        ),
        (
            "profile",
            "THREADS_TRANSPORT_FAILURE",
            "INBOUND_READ_FAILED",
            ("authenticated_profile", "profile_posts"),
        ),
        (
            "conversation",
            "THREADS_TRANSPORT_FAILURE",
            "INBOUND_READ_FAILED",
            ("authenticated_profile", "profile_posts", "conversation"),
        ),
    ],
)
async def test_configured_inbound_read_failure_is_persisted_and_stops_discovery(
    failure_stage: str,
    failure_code: str,
    expected_receipt_code: str,
    expected_calls: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    own_posts = (
        RemoteDiscoveryThread(
            "synthetic_owned_thread_001",
            "synthetic_owner_id",
            "configured_owner",
            "PRIVATE_SYNTHETIC_OWN_POST_TEXT",
            None,
            None,
            datetime.now(UTC),
            False,
            True,
        ),
    )
    api = _FakeApi(
        identity_error=_FakeApiError(failure_code) if failure_stage == "identity" else None,
        profile_error=_FakeApiError(failure_code) if failure_stage == "profile" else None,
        conversation_error=(
            _FakeApiError(failure_code) if failure_stage == "conversation" else None
        ),
        owned_posts=own_posts if failure_stage == "conversation" else (),
    )
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    with pytest.raises(session_module.RecruitmentSessionError) as error:
        await session_module.RecruitmentSession(tmp_path, accounts).observe(
            "alice",
            inbound_owner_username="configured_owner",
            inbound_thread_cap=1,
        )

    assert error.value.code == expected_receipt_code
    assert tuple(call[0] for call in api.calls) == expected_calls
    assert "search" not in tuple(call[0] for call in api.calls)
    assert "mentions" not in tuple(call[0] for call in api.calls)
    assert mutations.calls == []
    assert mutation_modes == [False]
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "FAILED"
    assert receipts[0]["error_code"] == expected_receipt_code
    assert all(
        private_value not in json.dumps(receipts[0])
        for private_value in ("configured_owner", "PRIVATE_SYNTHETIC_OWN_POST_TEXT")
    )


@pytest.mark.parametrize("effect", ("reply", "post"))
def test_owner_mismatch_blocks_approved_reply_and_post_from_script(
    effect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    api = _FakeApi(identity="foreign_owner")
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)
    private_terminal = _install_private_terminal(monkeypatch, "APPLY\n")
    if effect == "reply":
        packet = tmp_path / "synthetic-reply.json"
        _write_reply_packet(
            packet,
            fingerprint_remote_thread(api.thread.remote_thread_id),
            _REPLY_TEXT,
        )
        effect_args = ["--apply-reply-file", str(packet)]
    else:
        packet = tmp_path / "synthetic-candidate.json"
        _write_candidate_packet(packet)
        effect_args = ["--apply-post-file", str(packet)]

    status = session_module.main(
        [
            "--account",
            "alice",
            *effect_args,
            "--inbound-owner-username",
            "configured_owner",
            "--inbound-thread-cap",
            "1",
        ]
    )
    output = capsys.readouterr()

    assert status == 1
    assert output.out == ""
    assert output.err == ""
    assert private_terminal.getvalue() == "ERROR INBOUND_OWNERSHIP_MISMATCH\n"
    assert [call[0] for call in api.calls] == ["authenticated_profile"]
    assert mutations.calls == []
    assert mutation_modes == [False]
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "FAILED"
    assert receipts[0]["error_code"] == "INBOUND_OWNERSHIP_MISMATCH"
    assert _REPLY_TEXT not in json.dumps(receipts[0])
    assert _POST_TEXT not in json.dumps(receipts[0])


def test_cli_apply_requires_private_terminal_before_any_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)
    packet = tmp_path / "synthetic-post.json"
    _write_candidate_packet(packet)

    status = session_module.main(["--account", "alice", "--apply-post-file", str(packet)])
    output = capsys.readouterr()

    assert status == 1
    assert output.out == ""
    assert output.err == "ERROR PRIVATE_REVIEW_TERMINAL_REQUIRED\n"
    assert api.calls == []
    assert mutation_modes == []
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_apply_reply_uses_one_approved_exact_target_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)
    draft = NurtureDraft(
        target_fingerprint=fingerprint_remote_thread(api.thread.remote_thread_id),
        text="PRIVATE_SYNTHETIC_REPLY_TEXT",
    )

    result = await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)

    assert result.receipt is not None
    assert result.receipt.replied_count == 1
    assert result.receipt.decision_codes == ("REPLY_APPLIED",)
    assert mutation_modes == [True]
    assert [call[0] for call in mutations.calls] == ["reply"]


@pytest.mark.asyncio
async def test_apply_reply_revalidates_exact_target_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    _install_fake_app(monkeypatch, api, mutations)
    draft = NurtureDraft(
        target_fingerprint="0" * 64,
        text="PRIVATE_SYNTHETIC_REPLY_TEXT",
    )

    with pytest.raises(session_module.RecruitmentSessionError) as error:
        await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)

    assert error.value.code == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_apply_post_uses_one_approved_own_content_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    result = await session_module.RecruitmentSession(tmp_path, accounts).apply_post(
        "alice", _candidate()
    )

    assert result.receipt is not None
    assert result.receipt.published_count == 1
    assert result.receipt.decision_codes[-1] == "CONTENT_PUBLISHED"
    assert mutation_modes == [True]
    assert [call[0] for call in mutations.calls] == ["post"]


@pytest.mark.asyncio
async def test_confirmed_reply_is_not_repeated_after_session_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations()
    _install_fake_app(monkeypatch, api, mutations)
    draft = NurtureDraft(
        target_fingerprint=fingerprint_remote_thread(api.thread.remote_thread_id),
        text=_REPLY_TEXT,
    )

    first = await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)
    assert first.receipt is not None
    assert first.receipt.outcome == "SUCCESS"

    with pytest.raises(session_module.RecruitmentSessionError):
        await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)

    assert [call[0] for call in mutations.calls] == ["reply"]
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 2
    assert sum(receipt["outcome"] == "SUCCESS" for receipt in receipts) == 1
    assert _REPLY_TEXT not in json.dumps(receipts)


@pytest.mark.asyncio
async def test_ambiguous_reply_stays_terminal_after_session_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    mutations = _FakeMutations(
        reply_failure=StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", uuid4())
    )
    _install_fake_app(monkeypatch, api, mutations)
    draft = NurtureDraft(
        target_fingerprint=fingerprint_remote_thread(api.thread.remote_thread_id),
        text=_REPLY_TEXT,
    )

    with pytest.raises(session_module.RecruitmentSessionError) as first_error:
        await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)
    assert first_error.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "AMBIGUOUS"
    assert receipts[0]["error_code"] == "PUBLISH_OUTCOME_AMBIGUOUS"

    with pytest.raises(session_module.RecruitmentSessionError):
        await session_module.RecruitmentSession(tmp_path, accounts).apply_reply("alice", draft)

    assert [call[0] for call in mutations.calls] == ["reply"]
    assert _REPLY_TEXT not in json.dumps(_saved_receipts(tmp_path, accounts.get("alice").id))


@pytest.mark.asyncio
async def test_cancelled_observe_persists_interruption_and_can_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    search_entered = asyncio.Event()
    search_release = asyncio.Event()
    api = _FakeApi(search_entered=search_entered, search_release=search_release)
    mutations = _FakeMutations()
    _install_fake_app(monkeypatch, api, mutations)
    session = session_module.RecruitmentSession(tmp_path, accounts)

    interrupted_run = asyncio.create_task(session.observe("alice"))
    await asyncio.wait_for(search_entered.wait(), timeout=5)
    interrupted_run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted_run

    receipts = _saved_receipts(tmp_path, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "INTERRUPTED"
    assert receipts[0]["error_code"] == "INTERRUPTED"

    searches_before_restart = [call for call in api.calls if call[0] == "search"]
    search_release.set()
    restarted = await session_module.RecruitmentSession(tmp_path, accounts).observe("alice")
    assert restarted.receipt is not None
    assert restarted.receipt.outcome == "SUCCESS"
    searches_after_restart = [call for call in api.calls if call[0] == "search"]
    assert len(searches_after_restart) > len(searches_before_restart)
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_account_lock_contention_isolated_to_that_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    accounts.add("bob")
    api = _FakeApi()
    mutations = _FakeMutations()
    _install_fake_app(monkeypatch, api, mutations)
    session = session_module.RecruitmentSession(tmp_path, accounts)
    store = NurtureStore(tmp_path)

    with store.acquire_account_lock(accounts.get("alice").id):
        with pytest.raises(session_module.RecruitmentSessionError) as busy:
            await session.observe("alice")
        assert busy.value.code == "NURTURE_BUSY"
        assert api.calls == []

        bob = await session.observe("bob")
        assert bob.receipt is not None
        assert bob.receipt.outcome == "SUCCESS"

    search_aliases = [call[1] for call in api.calls if call[0] == "search"]
    assert search_aliases
    assert set(search_aliases) == {"bob"}
    assert mutations.calls == []


def test_cli_requires_exactly_one_session_or_apply_mode() -> None:
    with pytest.raises(SystemExit):
        session_module._build_parser().parse_args(
            [
                "--account",
                "alice",
                "--apply-reply-file",
                "synthetic-reply.json",
                "--apply-post-file",
                "synthetic-post.json",
            ]
        )
