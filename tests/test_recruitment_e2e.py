from __future__ import annotations

import asyncio
import io
import json
import os
import runpy
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

import threads_platform.standalone.nurture_store as nurture_store_module
import threads_platform.standalone.recruitment_session as recruitment_module
from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.mutations import (
    LocalOperationStore,
    LocalThreadsMutationRuntime,
)
from threads_platform.standalone.nurture_store import fingerprint_remote_thread

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_recruitment.py"
_TOKEN = "synthetic-rs05-token"
_TOKEN_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_RS05_TEST"
_REMOTE_TEXT = "Synthetic career question?"
_REPLY_TEXT = "Synthetic reply draft for E2E."
_POST_TEXT = "Synthetic own-content post for E2E."
_AVAILABLE_QUOTA = PublishingQuota(usage=1, total=100, reply_usage=1, reply_total=50)
_THREAD = RemoteDiscoveryThread(
    "synthetic_target_a",
    "synthetic_author_id",
    "synthetic_author",
    _REMOTE_TEXT,
    None,
    None,
    datetime(2026, 10, 10, tzinfo=UTC),
    False,
    True,
)
_OWN_THREAD = RemoteDiscoveryThread(
    "synthetic_owned_thread",
    "synthetic_owner_id",
    "synthetic_owner",
    "Synthetic owned post",
    None,
    None,
    datetime(2026, 10, 10, tzinfo=UTC),
    False,
    True,
)


class _TtyInput(io.StringIO):
    def __init__(self, value: str, events: list[str] | None = None) -> None:
        super().__init__(value)
        self.events = events

    def isatty(self) -> bool:
        return True

    def readline(self, size: int = -1) -> str:
        if self.events is not None:
            self.events.append("consent")
        return super().readline(size)


class _TtyOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _SyntheticResolver:
    def __init__(self, *, error: ThreadsCredentialError | None = None) -> None:
        self.error = error

    async def resolve(self, credential_ref: str) -> SecretStr:
        assert credential_ref == _TOKEN_REF
        if self.error is not None:
            raise self.error
        return SecretStr(_TOKEN)


class _SyntheticThreadsAPI:
    def __init__(
        self,
        root: Path,
        *,
        quota: PublishingQuota = _AVAILABLE_QUOTA,
        identity_username: str = "synthetic_owner",
        identity_error: ThreadsAPIError | None = None,
        profile_error: ThreadsAPIError | None = None,
        conversation_error: ThreadsAPIError | None = None,
        owned_posts: tuple[RemoteDiscoveryThread, ...] = (),
        search_threads: tuple[RemoteDiscoveryThread, ...] = (_THREAD,),
        search_error: ThreadsAPIError | None = None,
        search_entered: asyncio.Event | None = None,
        search_release: asyncio.Event | None = None,
        publish_error: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.root = root
        self.quota_value = quota
        self.identity_username = identity_username
        self.identity_error = identity_error
        self.profile_error = profile_error
        self.conversation_error = conversation_error
        self.owned_posts = owned_posts
        self.search_results = search_threads
        self.search_error = search_error
        self.search_entered = search_entered
        self.search_release = search_release
        self.publish_error = publish_error
        self.events = events if events is not None else []
        self.calls: list[str] = []
        self.remote_mutations: list[str] = []

    def _check_token(self, token: SecretStr) -> None:
        assert type(token) is SecretStr
        assert token.get_secret_value() == _TOKEN

    async def get_authenticated_profile(self, token: SecretStr) -> RemoteAuthenticatedProfile:
        self._check_token(token)
        self.calls.append("identity")
        self.events.append("identity")
        if self.identity_error is not None:
            raise self.identity_error
        return RemoteAuthenticatedProfile("synthetic_owner_id", self.identity_username)

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        self._check_token(token)
        self.calls.append("quota")
        self.events.append("quota")
        return self.quota_value

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
        self.calls.append("mentions")
        self.events.append("mentions")
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
        self.calls.append("search")
        self.events.append("search")
        if self.search_entered is not None:
            self.search_entered.set()
        if self.search_release is not None:
            await self.search_release.wait()
        if self.search_error is not None:
            raise self.search_error
        return DiscoveryPage(self.search_results, None, False)

    async def get_profile_posts(
        self,
        token: SecretStr,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self._check_token(token)
        assert username == self.identity_username and after is None and 1 <= limit <= 10
        self.calls.append("profile")
        self.events.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return DiscoveryPage(self.owned_posts, None, False)

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage:
        self._check_token(token)
        assert after is None
        self.calls.append("conversation")
        self.events.append("conversation")
        if self.conversation_error is not None:
            raise self.conversation_error
        assert thread_id == _OWN_THREAD.remote_thread_id
        return ReplyPage((), None, False)

    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        self._check_token(token)
        assert request.media_type == "TEXT"
        assert request.text in {_REPLY_TEXT, _POST_TEXT}
        self.calls.append("create_container")
        self.events.append("create_container")
        self.remote_mutations.append("create_container")
        journals = tuple((self.root / "operations").glob("*.json"))
        assert len(journals) == 1
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "RECEIVED"
        assert request.text not in journals[0].read_text(encoding="utf-8")
        if request.text == _REPLY_TEXT:
            assert request.reply_to_id == _THREAD.remote_thread_id
        return MediaContainer("synthetic_container")

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        self._check_token(token)
        assert container_id == "synthetic_container"
        self.calls.append("publish_container")
        self.events.append("publish_container")
        self.remote_mutations.append("publish_container")
        journals = tuple((self.root / "operations").glob("*.json"))
        assert len(journals) == 1
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "PUBLISH_REQUESTED"
        assert _TOKEN not in journals[0].read_text(encoding="utf-8")
        assert _REPLY_TEXT not in journals[0].read_text(encoding="utf-8")
        assert _POST_TEXT not in journals[0].read_text(encoding="utf-8")
        if self.publish_error:
            raise OSError("synthetic transport detail must not escape")
        return "synthetic_media_id"


def _install_synthetic_app(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: _SyntheticThreadsAPI,
    *,
    resolver: _SyntheticResolver | None = None,
) -> list[bool]:
    accounts = LocalAccountStore(root)
    app_resolver = resolver if resolver is not None else _SyntheticResolver()
    mutation_modes: list[bool] = []

    @asynccontextmanager
    async def fake_builder(
        app_root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = False,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert app_root.resolve() == root.resolve()
        assert local_accounts.get("alice").id == accounts.get("alice").id
        assert include_api is True
        assert include_browser is False
        mutation_modes.append(include_mutations)
        api_runtime = LocalThreadsApiRuntime(local_accounts, api, app_resolver)  # type: ignore[arg-type]
        mutations = (
            LocalThreadsMutationRuntime(
                app_root,
                local_accounts,
                api,  # type: ignore[arg-type]
                app_resolver,  # type: ignore[arg-type]
                LocalOperationStore(app_root),
            )
            if include_mutations
            else None
        )
        yield StandaloneAppContext(local_accounts, api_runtime, mutations)

    monkeypatch.setattr(recruitment_module, "build_standalone_app", fake_builder)
    return mutation_modes


def _write_reply(path: Path, text: str = _REPLY_TEXT, target: str | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "REPLY",
                "target_fingerprint": target or fingerprint_remote_thread(_THREAD.remote_thread_id),
                "text": text,
            }
        ),
        encoding="utf-8",
    )


def _write_post(path: Path, text: str = _POST_TEXT) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "POST_TEXT",
                "candidate_id": str(uuid4()),
                "account": "alice",
                "preset": "recruitment",
                "source_kind": "OPERATOR_SOURCE_ID",
                "source_id": "synthetic_source_rs05",
                "category": "CAREER_TIP",
                "text": text,
            }
        ),
        encoding="utf-8",
    )


def _run_script(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), *args])
    try:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    except SystemExit as error:
        return int(error.code)
    pytest.fail("recruitment entry script did not exit through its CLI boundary")


def _private_terminal(
    monkeypatch: pytest.MonkeyPatch,
    response: str = "",
    *,
    events: list[str] | None = None,
) -> _TtyOutput:
    output = _TtyOutput()
    monkeypatch.setattr(recruitment_module.sys, "stdin", _TtyInput(response, events))
    monkeypatch.setattr(recruitment_module.sys, "stderr", output)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return output


def _receipts(root: Path, account_id: object) -> list[dict[str, Any]]:
    directory = root / "nurture" / "runs" / str(account_id)
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.rglob("*.json")]


def _account(root: Path, *, credential: bool = True, alias: str = "alice") -> LocalAccountStore:
    accounts = LocalAccountStore(root)
    accounts.add(alias)
    if credential:
        accounts.set_credential_ref(alias, _TOKEN_REF)
    return accounts


class _FakeWatchTiming:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 11, tzinfo=UTC)
        self.monotonic_value = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def utc_now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.monotonic_value += seconds
        self.current += timedelta(seconds=seconds)


def _inject_watch_timing(
    monkeypatch: pytest.MonkeyPatch,
    timing: _FakeWatchTiming,
) -> None:
    original = recruitment_module.RecruitmentSession.watch_observe

    async def watch_with_fake_timing(
        session: recruitment_module.RecruitmentSession,
        account_alias: str,
        **kwargs: object,
    ) -> object:
        kwargs["timing"] = timing
        return await original(session, account_alias, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        recruitment_module.RecruitmentSession,
        "watch_observe",
        watch_with_fake_timing,
    )


def test_real_script_validate_is_zero_write_and_proves_wheel_import_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    root.mkdir()
    _account(root)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)

    assert _run_script(monkeypatch, ["--account", "alice", "--validate"]) == 0
    result = capsys.readouterr()
    assert result.out == "recruitment-session mode=validate status=READY\n"
    assert result.err == ""
    assert not api.calls and not mutation_modes
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before
    assert not (root / "nurture").exists()
    assert _SCRIPT.is_file()
    assert "from threads_platform.standalone.recruitment_session import main" in _SCRIPT.read_text(
        encoding="utf-8"
    )
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(recruitment_module.__file__).resolve()).casefold()


def test_real_script_preflight_rejects_missing_credential_without_remote_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root, credential=False)
    api = _SyntheticThreadsAPI(root)
    _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert _run_script(monkeypatch, ["--account", "alice", "--preflight"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR THREADS_CREDENTIAL_NOT_CONFIGURED\n"
    assert not api.calls
    assert not (root / "nurture").exists()
    assert accounts.get("alice").credential_ref is None


def test_real_script_preflight_rejects_expired_secret_reference_without_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(
        root,
        monkeypatch,
        api,
        resolver=_SyntheticResolver(
            error=ThreadsCredentialError(ThreadsCredentialErrorCode.EXPIRED)
        ),
    )
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert _run_script(monkeypatch, ["--account", "alice", "--preflight"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR THREADS_CREDENTIAL_EXPIRED\n"
    assert not api.calls and mutation_modes == [False]
    assert not (root / "nurture").exists()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("THREADS_AUTHENTICATION_FAILED", "THREADS_AUTHENTICATION_FAILED"),
        ("THREADS_PERMISSION_DENIED", "THREADS_PERMISSION_DENIED"),
    ],
)
def test_real_script_preflight_fails_closed_on_authentication_and_permission_errors(
    code: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root, identity_error=ThreadsAPIError(code))
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert _run_script(monkeypatch, ["--account", "alice", "--preflight"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"ERROR {expected}\n"
    assert api.calls == ["identity"]
    assert mutation_modes == [False]
    assert not (root / "nurture").exists()


@pytest.mark.parametrize(
    ("quota", "status", "post", "reply", "expected_exit"),
    [
        (
            PublishingQuota(usage=2, total=100, reply_usage=1, reply_total=50),
            "OBSERVED",
            "available",
            "available",
            0,
        ),
        (
            PublishingQuota(usage=100, total=100, reply_usage=1, reply_total=50),
            "STOP",
            "exhausted",
            "available",
            1,
        ),
        (
            PublishingQuota(usage=2, total=100, reply_usage=None, reply_total=None),
            "STOP",
            "available",
            "unverified",
            1,
        ),
        (
            PublishingQuota(usage=2, total=100, reply_usage=50, reply_total=50),
            "STOP",
            "available",
            "exhausted",
            1,
        ),
    ],
)
def test_real_script_preflight_reports_bounded_quota_snapshot(
    quota: PublishingQuota,
    status: str,
    post: str,
    reply: str,
    expected_exit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root, quota=quota)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert _run_script(monkeypatch, ["--account", "alice", "--preflight"]) == expected_exit
    output = capsys.readouterr()
    assert output.out == (
        "recruitment-session mode=preflight "
        f"status={status} credential=verified post_quota={post} reply_quota={reply}\n"
    )
    assert output.err == ""
    assert api.calls == ["identity", "quota"]
    assert mutation_modes == [False]
    assert not (root / "nurture").exists()
    assert _TOKEN not in output.out
    assert "synthetic_owner" not in output.out


def test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    timing = _FakeWatchTiming()
    _inject_watch_timing(monkeypatch, timing)

    real_asyncio_run = asyncio.run
    run_calls: list[object] = []

    def count_asyncio_run(awaitable: object, **kwargs: object) -> object:
        run_calls.append(awaitable)
        return real_asyncio_run(awaitable, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recruitment_module.asyncio, "run", count_asyncio_run)
    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                "--watch-observe",
                "--max-cycles",
                "3",
            ],
        )
        == 0
    )

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.count("watch cycle=") == 3
    assert "watch preflight status=OBSERVED" in output.out
    assert "inbound=INBOUND_NOT_CONFIGURED" in output.out
    assert output.out.endswith("watch shutdown status=COMPLETED cycles=3\n")
    assert len(run_calls) == 1
    assert timing.sleep_calls == [1_800, 1_800]
    assert api.calls.count("identity") == 1
    assert api.calls.count("quota") == 1
    assert api.calls.count("mentions") == 3
    assert api.calls.count("search") == 33
    assert mutation_modes == [False, False, False, False]
    assert api.remote_mutations == []
    assert not (root / "operations").exists()

    receipts = sorted(
        _receipts(root, accounts.get("alice").id),
        key=lambda receipt: receipt["started_at"],
    )
    assert len(receipts) == 3
    assert all(receipt["outcome"] == "SUCCESS" for receipt in receipts)
    assert sum(receipt["selected_count"] for receipt in receipts) == 1
    assert all(
        set(receipt)
        == {
            "version",
            "id",
            "account_id",
            "preset_id",
            "preset_version",
            "started_at",
            "finished_at",
            "outcome",
            "discovered_count",
            "deduped_count",
            "selected_count",
            "enriched_count",
            "replied_count",
            "published_count",
            "skipped_count",
            "decision_codes",
            "operation_ids",
            "error_code",
            "failed_stage",
        }
        for receipt in receipts
    )

    private_markers = (
        _TOKEN,
        _TOKEN_REF,
        _REMOTE_TEXT,
        _THREAD.remote_thread_id,
        "synthetic_author",
        "https://threads.example/private",
    )
    assert all(marker not in output.out + output.err for marker in private_markers)
    persisted_nurture = "".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture").rglob("*.json")
    )
    assert all(marker not in persisted_nurture for marker in private_markers)


def test_real_script_watch_checks_inbound_binding_and_stops_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(
        root,
        identity_username="foreign_synthetic_owner",
        owned_posts=(_OWN_THREAD,),
    )
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    _inject_watch_timing(monkeypatch, _FakeWatchTiming())

    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                "--watch-observe",
                "--max-cycles",
                "3",
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ],
        )
        == 1
    )

    output = capsys.readouterr()
    assert "reason=INBOUND_OWNERSHIP_MISMATCH inbound=UNAVAILABLE" in output.out
    assert output.out.endswith(
        "watch shutdown status=STOP cycles=1 reason=INBOUND_OWNERSHIP_MISMATCH\n"
    )
    assert "search" not in api.calls
    assert "mentions" not in api.calls
    assert api.calls == ["identity", "quota", "identity"]
    assert mutation_modes == [False, False]
    assert api.remote_mutations == []


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("profile", "INBOUND_READ_FAILED"),
        ("permission", "INBOUND_PERMISSION_MISSING"),
        ("conversation", "INBOUND_READ_FAILED"),
        ("discovery", "THREADS_RATE_LIMITED"),
    ],
)
def test_real_script_watch_fails_closed_on_read_errors(
    failure: str,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(
        root,
        owned_posts=(_OWN_THREAD,),
        profile_error=(
            ThreadsAPIError(
                "THREADS_PERMISSION_DENIED"
                if failure == "permission"
                else "THREADS_TRANSPORT_FAILURE"
            )
            if failure in {"profile", "permission"}
            else None
        ),
        conversation_error=(
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE") if failure == "conversation" else None
        ),
        search_error=(ThreadsAPIError("THREADS_RATE_LIMITED") if failure == "discovery" else None),
    )
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    _inject_watch_timing(monkeypatch, _FakeWatchTiming())
    args = ["--account", "alice", "--watch-observe", "--max-cycles", "3"]
    if failure != "discovery":
        args.extend(
            [
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ]
        )

    assert _run_script(monkeypatch, args) == 1
    output = capsys.readouterr()
    assert f"reason={expected_code} inbound=UNAVAILABLE" in output.out
    assert output.out.endswith(f"watch shutdown status=STOP cycles=1 reason={expected_code}\n")
    assert api.remote_mutations == []
    assert mutation_modes == [False] * len(mutation_modes)
    assert len(mutation_modes) == 2
    if failure in {"profile", "permission", "conversation"}:
        assert "search" not in api.calls
        assert "mentions" not in api.calls
    else:
        assert api.calls.count("search") == 1


def test_real_script_watch_quota_stop_prevents_first_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(
        root,
        quota=PublishingQuota(usage=100, total=100, reply_usage=1, reply_total=50),
    )
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "1"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == (
        "watch preflight status=STOP credential=verified post_quota=exhausted "
        "reply_quota=available\n"
        "watch shutdown status=STOP cycles=0 reason=PREFLIGHT_STOP\n"
    )
    assert api.calls == ["identity", "quota"]
    assert mutation_modes == [False]
    assert not (root / "nurture" / "runs").exists()


def test_real_script_watch_help_and_mode_exclusivity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_script(monkeypatch, ["--help"]) == 0
    help_output = capsys.readouterr()
    assert "--watch-observe" in help_output.out
    assert "--interval-minutes" in help_output.out
    assert "--max-cycles" in help_output.out

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--observe"],
        )
        == 2
    )
    exclusive = capsys.readouterr()
    assert "not allowed with argument" in exclusive.err

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--interval-minutes", "14"],
        )
        == 2
    )
    invalid_interval = capsys.readouterr()
    assert "watch interval must be an integer from 15 to 1440 minutes" in invalid_interval.err

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--observe", "--max-cycles", "2"],
        )
        == 1
    )
    non_watch = capsys.readouterr()
    assert non_watch.err == "ERROR WATCH_OPTIONS_REQUIRE_WATCH_MODE\n"


def test_real_script_watch_reports_verified_inbound_capability_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root, owned_posts=(_OWN_THREAD,))
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    _inject_watch_timing(monkeypatch, _FakeWatchTiming())

    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                "--watch-observe",
                "--max-cycles",
                "1",
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ],
        )
        == 0
    )
    output = capsys.readouterr()
    assert "inbound=INBOUND_READY" in output.out
    assert "Synthetic owned post" not in output.out
    assert "synthetic_owner" not in output.out
    assert api.calls.count("profile") == 1
    assert api.calls.count("conversation") == 1
    assert (
        api.calls.index("profile") < api.calls.index("conversation") < api.calls.index("mentions")
    )
    assert mutation_modes == [False, False]
    assert api.remote_mutations == []
    persisted_nurture = "".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture").rglob("*.json")
    )
    assert "synthetic_owner" not in persisted_nurture


@pytest.mark.parametrize(
    ("credential", "identity_error", "expected_code", "expected_calls"),
    [
        (False, None, "THREADS_CREDENTIAL_NOT_CONFIGURED", []),
        (True, "THREADS_AUTHENTICATION_FAILED", "THREADS_AUTHENTICATION_FAILED", ["identity"]),
        (True, "THREADS_PERMISSION_DENIED", "THREADS_PERMISSION_DENIED", ["identity"]),
        (True, "THREADS_RATE_LIMITED", "THREADS_RATE_LIMITED", ["identity"]),
    ],
)
def test_real_script_watch_stops_on_preflight_authentication_failure(
    credential: bool,
    identity_error: str | None,
    expected_code: str,
    expected_calls: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root, credential=credential)
    api = _SyntheticThreadsAPI(
        root,
        identity_error=None if identity_error is None else ThreadsAPIError(identity_error),
    )
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "2"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out.endswith(f"watch shutdown status=STOP cycles=0 reason={expected_code}\n")
    assert api.calls == expected_calls
    assert mutation_modes == [False]
    assert api.remote_mutations == []
    assert "synthetic private" not in output.out + output.err


def test_real_script_watch_stops_on_expired_credential_before_api_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(
        root,
        monkeypatch,
        api,
        resolver=_SyntheticResolver(
            error=ThreadsCredentialError(ThreadsCredentialErrorCode.EXPIRED)
        ),
    )
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "2"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out.endswith(
        "watch shutdown status=STOP cycles=0 reason=THREADS_CREDENTIAL_EXPIRED\n"
    )
    assert api.calls == []
    assert mutation_modes == [False]
    assert api.remote_mutations == []


def test_watch_cancellation_persists_interrupted_receipt_and_allows_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    entered = asyncio.Event()
    release = asyncio.Event()
    api = _SyntheticThreadsAPI(root, search_entered=entered, search_release=release)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    async def cancel_during_read() -> object:
        session = recruitment_module.RecruitmentSession(root)
        pending = asyncio.create_task(
            session.watch_observe(
                "alice",
                interval_minutes=15,
                max_cycles=3,
                timing=_FakeWatchTiming(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        return await pending

    interrupted = asyncio.run(cancel_during_read())
    assert interrupted.status == "INTERRUPTED"
    assert interrupted.cycles == 1
    output = capsys.readouterr()
    assert output.out.endswith("watch shutdown status=INTERRUPTED cycles=1 reason=INTERRUPTED\n")
    receipts = _receipts(root, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "INTERRUPTED"
    assert receipts[0]["error_code"] == "INTERRUPTED"
    assert api.remote_mutations == []

    release.set()
    restarted = asyncio.run(
        recruitment_module.RecruitmentSession(root).watch_observe(
            "alice",
            interval_minutes=15,
            max_cycles=1,
            timing=_FakeWatchTiming(),
        )
    )
    assert restarted.status == "COMPLETED"
    assert restarted.cycles == 1
    assert len(_receipts(root, accounts.get("alice").id)) == 2
    assert mutation_modes == [False, False, False, False]
    assert api.remote_mutations == []


def test_real_script_watch_missing_root_stops_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_root = tmp_path / "synthetic-missing-root"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(missing_root))

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "1"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ("watch shutdown status=STOP cycles=0 reason=LOCAL_DATA_ROOT_NOT_FOUND\n")
    assert output.err == ""
    assert not missing_root.exists()


def test_real_script_watch_malformed_v1_state_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    account_id = accounts.get("alice").id
    state_directory = root / "nurture" / "state" / str(account_id)
    state_directory.mkdir(parents=True)
    (state_directory / "recruitment.json").write_text("not valid json", encoding="utf-8")
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "3"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out.endswith("watch shutdown status=STOP cycles=1 reason=NURTURE_STATE_INVALID\n")
    assert "search" not in api.calls and "mentions" not in api.calls
    assert mutation_modes == [False, False]
    assert api.remote_mutations == []


def test_real_script_watch_synthetic_full_disk_failure_stops_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    def fail_new_temporary_file(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "synthetic full disk private detail")

    monkeypatch.setattr(nurture_store_module.tempfile, "mkstemp", fail_new_temporary_file)
    assert (
        _run_script(
            monkeypatch,
            ["--account", "alice", "--watch-observe", "--max-cycles", "2"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out.endswith("watch shutdown status=STOP cycles=1 reason=NURTURE_STATE_INVALID\n")
    assert "synthetic full disk private detail" not in output.out + output.err
    assert "search" not in api.calls and "mentions" not in api.calls
    assert mutation_modes == [False, False]
    assert api.remote_mutations == []


def test_real_script_no_config_preserves_legacy_discovery_and_persists_success_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert _run_script(monkeypatch, ["--account", "alice", "--observe"]) == 0
    output = capsys.readouterr()
    receipts = _receipts(root, accounts.get("alice").id)
    assert "status=SUCCESS" in output.out and "inbound=INBOUND_NOT_CONFIGURED" in output.out
    assert api.calls.count("search") > 0 and "mentions" in api.calls
    assert "identity" not in api.calls
    assert mutation_modes == [False]
    assert len(receipts) == 1 and receipts[0]["outcome"] == "SUCCESS"
    assert set(receipts[0]) == {
        "version",
        "id",
        "account_id",
        "preset_id",
        "preset_version",
        "started_at",
        "finished_at",
        "outcome",
        "discovered_count",
        "deduped_count",
        "selected_count",
        "enriched_count",
        "replied_count",
        "published_count",
        "skipped_count",
        "decision_codes",
        "operation_ids",
        "error_code",
        "failed_stage",
    }
    assert receipts[0]["version"] == 1
    assert not api.remote_mutations


def test_real_script_enabled_inbound_reads_only_the_bound_owner_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root, owned_posts=(_OWN_THREAD,))
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                "--observe",
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ],
        )
        == 0
    )
    output = capsys.readouterr()
    receipts = _receipts(root, accounts.get("alice").id)
    assert api.calls[:3] == ["identity", "profile", "conversation"]
    assert "search" in api.calls and "mentions" in api.calls
    assert "inbound=INBOUND_READY" in output.out
    assert mutation_modes == [False]
    assert len(receipts) == 1 and receipts[0]["outcome"] == "SUCCESS"
    assert "synthetic_owner" not in json.dumps(receipts)
    assert not api.remote_mutations


@pytest.mark.parametrize(
    ("stage", "expected_calls", "status"),
    [
        ("identity", ["identity"], "INBOUND_OWNERSHIP_MISMATCH"),
        ("profile", ["identity", "profile"], "INBOUND_READ_FAILED"),
        ("conversation", ["identity", "profile", "conversation"], "INBOUND_READ_FAILED"),
    ],
)
def test_real_script_configured_inbound_failure_persists_failed_receipt_and_never_falls_back(
    stage: str,
    expected_calls: list[str],
    status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    kwargs: dict[str, Any] = {
        "identity_username": "foreign_synthetic_owner"
        if stage == "identity"
        else "synthetic_owner",
        "profile_error": ThreadsAPIError("THREADS_TRANSPORT_FAILURE")
        if stage == "profile"
        else None,
        "conversation_error": (
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE") if stage == "conversation" else None
        ),
        "owned_posts": (_OWN_THREAD,) if stage == "conversation" else (),
    }
    api = _SyntheticThreadsAPI(root, **kwargs)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                "--observe",
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ],
        )
        == 1
    )
    output = capsys.readouterr()
    receipts = _receipts(root, accounts.get("alice").id)
    assert output.out == ""
    assert output.err.startswith("ERROR ")
    assert api.calls == expected_calls
    assert "search" not in api.calls and "mentions" not in api.calls
    assert mutation_modes == [False]
    assert not api.remote_mutations
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "FAILED"
    assert receipts[0]["error_code"] == status
    assert "synthetic_owner" not in json.dumps(receipts)


def test_real_script_reply_wrong_target_does_not_switch_candidate_or_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    draft = tmp_path / "synthetic-reply.json"
    wrong_fingerprint = fingerprint_remote_thread("synthetic_target_b")
    _write_reply(draft, target=wrong_fingerprint)

    assert _run_script(monkeypatch, ["--account", "alice", "--apply-reply-file", str(draft)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    assert "ERROR NURTURE_DRAFT_TARGET_MISMATCH" in private.getvalue()
    assert api.remote_mutations == []
    assert not (root / "operations").exists()
    assert mutation_modes == [False]
    receipts = _receipts(root, accounts.get("alice").id)
    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "FAILED"
    assert receipts[0]["error_code"] == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert _REPLY_TEXT not in json.dumps(receipts)


@pytest.mark.parametrize("effect", ("reply", "post"))
def test_real_script_foreign_inbound_identity_blocks_approved_candidate(
    effect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root, identity_username="foreign_synthetic_owner")
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    if effect == "reply":
        packet = tmp_path / "synthetic-reply.json"
        _write_reply(packet)
        effect_args = ["--apply-reply-file", str(packet)]
    else:
        packet = tmp_path / "synthetic-post.json"
        _write_post(packet)
        effect_args = ["--apply-post-file", str(packet)]

    assert (
        _run_script(
            monkeypatch,
            [
                "--account",
                "alice",
                *effect_args,
                "--inbound-owner-username",
                "synthetic_owner",
                "--inbound-thread-cap",
                "1",
            ],
        )
        == 1
    )
    output = capsys.readouterr()
    receipts = _receipts(root, accounts.get("alice").id)
    assert output.out == "" and output.err == ""
    assert "ERROR INBOUND_OWNERSHIP_MISMATCH" in private.getvalue()
    assert api.calls == ["identity"]
    assert api.remote_mutations == []
    assert mutation_modes == [False]
    assert len(receipts) == 1 and receipts[0]["outcome"] == "FAILED"
    assert _POST_TEXT not in json.dumps(receipts)
    assert _REPLY_TEXT not in json.dumps(receipts)


def test_real_script_apply_rechecks_quota_before_reservation_or_remote_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(
        root,
        quota=PublishingQuota(usage=100, total=100, reply_usage=1, reply_total=50),
        search_threads=(),
    )
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    packet = tmp_path / "synthetic-post.json"
    _write_post(packet)

    assert _run_script(monkeypatch, ["--account", "alice", "--apply-post-file", str(packet)]) == 1
    output = capsys.readouterr()
    receipts = _receipts(root, accounts.get("alice").id)
    assert output.out == "" and output.err == ""
    assert "ERROR THREADS_PUBLISHING_QUOTA_REACHED" in private.getvalue()
    assert api.remote_mutations == []
    assert not (root / "operations").exists()
    assert mutation_modes == [False, True]
    assert any(
        receipt["outcome"] == "FAILED"
        and receipt["error_code"] == "THREADS_PUBLISHING_QUOTA_REACHED"
        for receipt in receipts
    )


def test_real_script_private_review_and_declined_consent_never_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    events: list[str] = []
    api = _SyntheticThreadsAPI(root, events=events, search_threads=())
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "NO\n", events=events)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    packet = tmp_path / "synthetic-post.json"
    _write_post(packet)

    assert _run_script(monkeypatch, ["--account", "alice", "--apply-post-file", str(packet)]) == 0
    output = capsys.readouterr()
    private_text = private.getvalue()
    assert "status=DECLINED" in output.out
    assert _POST_TEXT not in output.out and _REMOTE_TEXT not in output.out
    assert "action: POST_TEXT" in private_text
    assert 'source id: "synthetic_source_rs05"' in private_text
    assert f"proposed post: {json.dumps(_POST_TEXT)}" in private_text
    assert events.index("consent") < len(events)
    assert "create_container" not in events and not api.remote_mutations
    assert mutation_modes == [False]
    receipts = _receipts(root, accounts.get("alice").id)
    assert len(receipts) == 1 and receipts[0]["outcome"] == "SUCCESS"
    assert all(_POST_TEXT not in json.dumps(receipt) for receipt in receipts)


def test_real_script_approved_reply_uses_exact_review_and_real_operation_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    events: list[str] = []
    api = _SyntheticThreadsAPI(root, events=events)
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n", events=events)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    draft = tmp_path / "synthetic-reply.json"
    _write_reply(draft)

    assert _run_script(monkeypatch, ["--account", "alice", "--apply-reply-file", str(draft)]) == 0
    output = capsys.readouterr()
    journal_paths = tuple((root / "operations").glob("*.json"))
    assert len(journal_paths) == 1
    journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
    assert journal["version"] == 1
    assert journal["kind"] == "CREATE_REPLY" and journal["phase"] == "PUBLISHED"
    assert "mode=apply_reply status=SUCCESS" in output.out
    assert _REPLY_TEXT not in output.out and _REMOTE_TEXT not in output.out
    assert (
        f"fingerprint: {fingerprint_remote_thread(_THREAD.remote_thread_id)}" in private.getvalue()
    )
    assert "source: " in private.getvalue()
    assert "reason codes: " in private.getvalue()
    assert f"target context: {json.dumps(_REMOTE_TEXT)}" in private.getvalue()
    assert "action: REPLY" in private.getvalue()
    assert f"proposed reply: {json.dumps(_REPLY_TEXT)}" in private.getvalue()
    assert events.index("consent") < events.index("create_container")
    assert api.remote_mutations == ["create_container", "publish_container"]
    assert mutation_modes == [False, True]
    assert _TOKEN not in json.dumps(journal)
    assert _REPLY_TEXT not in json.dumps(journal)
    receipts = _receipts(root, accounts.get("alice").id)
    assert len(receipts) == 2
    assert all(_REPLY_TEXT not in json.dumps(receipt) for receipt in receipts)


def test_real_script_approved_own_content_is_independent_and_journaled_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    events: list[str] = []
    api = _SyntheticThreadsAPI(root, events=events, search_threads=())
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n", events=events)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    packet = tmp_path / "synthetic-post.json"
    _write_post(packet)

    assert _run_script(monkeypatch, ["--account", "alice", "--apply-post-file", str(packet)]) == 0
    output = capsys.readouterr()
    journal_paths = tuple((root / "operations").glob("*.json"))
    assert len(journal_paths) == 1
    journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
    assert journal["version"] == 1
    assert journal["kind"] == "POST_TEXT" and journal["phase"] == "PUBLISHED"
    assert "mode=apply_post status=SUCCESS" in output.out
    assert _POST_TEXT not in output.out
    assert "own-content effect:" in private.getvalue()
    assert "proposed post: " + json.dumps(_POST_TEXT) in private.getvalue()
    assert events.index("consent") < events.index("create_container")
    assert api.remote_mutations == ["create_container", "publish_container"]
    assert mutation_modes == [False, True]
    assert _POST_TEXT not in json.dumps(journal)
    assert all(
        _POST_TEXT not in json.dumps(receipt)
        for receipt in _receipts(root, accounts.get("alice").id)
    )


def test_real_script_ambiguous_publish_is_persisted_and_never_replayed_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-root"
    accounts = _account(root)
    api = _SyntheticThreadsAPI(root, publish_error=True, search_threads=())
    mutation_modes = _install_synthetic_app(root, monkeypatch, api)
    private = _private_terminal(monkeypatch, "APPLY\n")
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    packet = tmp_path / "synthetic-post.json"
    _write_post(packet)
    args = ["--account", "alice", "--apply-post-file", str(packet)]

    assert _run_script(monkeypatch, args) == 1
    first_output = capsys.readouterr()
    journal_paths = tuple((root / "operations").glob("*.json"))
    assert len(journal_paths) == 1
    journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "AMBIGUOUS"
    assert "PUBLISH_OUTCOME_AMBIGUOUS" in private.getvalue()
    first_remote_mutations = tuple(api.remote_mutations)
    assert first_remote_mutations == ("create_container", "publish_container")

    assert _run_script(monkeypatch, args) == 1
    second_output = capsys.readouterr()
    assert api.remote_mutations == list(first_remote_mutations)
    assert len(tuple((root / "operations").glob("*.json"))) == 1
    assert "synthetic transport detail" not in first_output.err + second_output.err
    assert _POST_TEXT not in first_output.out + second_output.out
    assert mutation_modes == [False, True, False]
    assert all(_TOKEN not in path.read_text(encoding="utf-8") for path in journal_paths)
    assert _POST_TEXT not in json.dumps(journal)
    receipts = _receipts(root, accounts.get("alice").id)
    assert sum(receipt["outcome"] == "AMBIGUOUS" for receipt in receipts) == 1
    assert sum(receipt["error_code"] == "PUBLISH_OUTCOME_AMBIGUOUS" for receipt in receipts) == 1
    assert _POST_TEXT not in json.dumps(receipts)
