from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
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
)
from threads_platform.standalone.nurture import get_nurture_preset
from threads_platform.standalone.nurture_content import ContentCandidateV1, ContentCategory
from threads_platform.standalone.nurture_draft import NurtureDraft
from threads_platform.standalone.nurture_store import fingerprint_remote_thread

_REMOTE_TEXT = "REMOTE_SYNTHETIC_TARGET_TEXT career question"
_POST_TEXT = "PRIVATE_SYNTHETIC_POST_TEXT"


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


class _FakeApi:
    def __init__(self, *, identity: str = "configured_owner") -> None:
        self.thread = _thread()
        self.identity = identity
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
        return DiscoveryPage((), None, True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.calls.append(("conversation", alias, thread_id, after))
        return ReplyPage((), None, True)

    async def authenticated_profile(self, alias: str) -> RemoteAuthenticatedProfile:
        self.calls.append(("authenticated_profile", alias))
        return RemoteAuthenticatedProfile("synthetic_owner_id", self.identity)

    async def quota(self, alias: str) -> PublishingQuota:
        self.calls.append(("quota", alias))
        return PublishingQuota(usage=0, total=100)


class _FakeMutations:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def create_reply(
        self,
        alias: str,
        thread_id: str,
        text: str,
        *,
        parent_reply_id: str | None = None,
    ) -> CreatedReplyResult:
        self.calls.append(("reply", alias, thread_id, text, parent_reply_id))
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

    completed = subprocess.run(
        [sys.executable, str(script), "--account", "alice", "--validate"],
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


def test_cli_review_precedes_apply_and_keeps_private_text_off_stdout(
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

    review_status = session_module.main(
        ["--account", "alice", "--review", "--review-post-file", str(packet)]
    )
    review_output = capsys.readouterr()
    assert review_status == 0
    assert "mode=review" in review_output.out
    assert "own_content=due" in review_output.out
    assert _POST_TEXT not in review_output.out
    assert _REMOTE_TEXT not in review_output.out
    assert mutation_modes == [False]
    assert mutations.calls == []

    apply_status = session_module.main(["--account", "alice", "--apply-post-file", str(packet)])
    apply_output = capsys.readouterr()
    assert apply_status == 0
    assert "mode=apply_post" in apply_output.out
    assert _POST_TEXT not in apply_output.out
    assert _REMOTE_TEXT not in apply_output.out
    assert mutation_modes == [False, True]
    assert [call[0] for call in mutations.calls] == ["post"]


@pytest.mark.asyncio
async def test_configured_owner_binding_is_reused_and_mismatch_fails_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    preset = replace(
        get_nurture_preset("recruitment"),
        owner_public_username="configured_owner",
        max_owned_threads_inspected_per_explicit_run=1,
    )
    monkeypatch.setattr(session_module, "get_nurture_preset", lambda _preset_id: preset)
    api = _FakeApi(identity="foreign_owner")
    mutations = _FakeMutations()
    mutation_modes = _install_fake_app(monkeypatch, api, mutations)

    with pytest.raises(session_module.RecruitmentSessionError) as error:
        await session_module.RecruitmentSession(tmp_path, accounts).observe("alice")

    assert error.value.code == "INBOUND_OWNERSHIP_MISMATCH"
    assert [call[0] for call in api.calls] == ["authenticated_profile"]
    assert mutations.calls == []
    assert mutation_modes == [False]
    receipt_files = tuple(
        (tmp_path / "nurture" / "runs" / str(accounts.get("alice").id)).rglob("*.json")
    )
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["outcome"] == "FAILED"
    assert receipt["error_code"] == "INBOUND_OWNERSHIP_MISMATCH"


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
