from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import threads_platform.standalone.recruitment_session as session_module
from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_runner import NurtureShortlistResult
from threads_platform.standalone.nurture_store import (
    NurtureActionState,
    NurtureStore,
    fingerprint_remote_thread,
)

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_SECRET_MARKER = "SYNTHETIC_PRIVATE_CONTENT_SENTINEL"
_REMOTE_USERNAME = "synthetic_author_private"
_REMOTE_URL = "https://example.invalid/synthetic-private"
_TOKEN_MARKER = "SYNTHETIC_TOKEN_SENTINEL"
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_recruitment_shortlist.py"


def _thread(
    remote_id: str,
    text: str,
    *,
    timestamp: datetime = _NOW - timedelta(hours=1),
) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_id,
        f"author-{remote_id}",
        _REMOTE_USERNAME,
        text,
        _REMOTE_URL,
        None,
        timestamp,
        False,
        False,
    )


class _FakeReadError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_TOKEN_MARKER + " private response detail")


class _FakeApi:
    def __init__(
        self,
        *,
        mentions: tuple[RemoteDiscoveryThread, ...] = (),
        search: tuple[RemoteDiscoveryThread, ...] = (),
        identity_username: str = "synthetic_owner",
        identity_error: Exception | None = None,
        profile_error: Exception | None = None,
        conversation_error: Exception | None = None,
        search_error: Exception | None = None,
        owned_posts: tuple[RemoteDiscoveryThread, ...] = (),
        replies: tuple[RemoteReply, ...] = (),
    ) -> None:
        self.mentions_value = mentions
        self.search_value = search
        self.identity_username = identity_username
        self.identity_error = identity_error
        self.profile_error = profile_error
        self.conversation_error = conversation_error
        self.search_error = search_error
        self.owned_posts = owned_posts
        self.replies = replies
        self.calls: list[str] = []

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        self.calls.append("identity")
        if self.identity_error is not None:
            raise self.identity_error
        return RemoteAuthenticatedProfile("synthetic-owner-id", self.identity_username)

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and 1 <= limit <= 50
        self.calls.append("mentions")
        return DiscoveryPage(self.mentions_value, None, False)

    async def search(
        self,
        _alias: str,
        _query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert search_mode in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
        assert search_type is DiscoverySearchType.RECENT
        assert after is None and 1 <= limit <= 50
        self.calls.append("search")
        if self.search_error is not None:
            raise self.search_error
        return DiscoveryPage(self.search_value, None, False)

    async def profile_posts(
        self,
        _alias: str,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert username == self.identity_username and after is None and 1 <= limit <= 10
        self.calls.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return DiscoveryPage(self.owned_posts, None, False)

    async def conversation(self, _alias: str, _thread_id: str, after: str | None) -> ReplyPage:
        assert after is None
        self.calls.append("conversation")
        if self.conversation_error is not None:
            raise self.conversation_error
        return ReplyPage(self.replies, None, False)


def _install_fake_app(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeApi,
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
        if include_mutations:
            raise AssertionError("shortlist must never construct mutation runtime")
        yield StandaloneAppContext(
            accounts,
            cast(LocalThreadsApiRuntime, api),
            None,
        )

    monkeypatch.setattr(session_module, "build_standalone_app", fake_builder)
    return mutation_modes


def _account_root(
    tmp_path: Path, aliases: tuple[str, ...] = ("alice",)
) -> tuple[
    Path,
    LocalAccountStore,
    tuple[LocalAccount, ...],
]:
    root = tmp_path / "synthetic-root"
    root.mkdir(parents=True)
    accounts = LocalAccountStore(root)
    return root, accounts, tuple(accounts.add(alias) for alias in aliases)


def _seed_target(
    root: Path,
    account: LocalAccount,
    preset: NurturePresetV1,
    remote_id: str,
    action_state: str,
) -> None:
    store = NurtureStore(root)
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW - timedelta(minutes=2))
        with scope:
            fingerprint = fingerprint_remote_thread(remote_id)
            if action_state == "NONE":
                owner.observe_target(
                    preset,
                    fingerprint,
                    scope.receipt.id,
                    "SYNTHETIC_PRIOR_OBSERVATION",
                    now=_NOW - timedelta(minutes=1),
                )
            else:
                owner.reserve_target(
                    preset,
                    fingerprint,
                    scope.receipt.id,
                    "SYNTHETIC_PRIOR_ACTION",
                    now=_NOW - timedelta(minutes=1),
                )
                if action_state in {"CONFIRMED", "AMBIGUOUS"}:
                    owner.complete_target_action(
                        preset,
                        fingerprint,
                        scope.receipt.id,
                        cast(NurtureActionState, action_state),
                        uuid4(),
                        now=_NOW - timedelta(minutes=1),
                    )
            scope.finish("SUCCESS", now=_NOW - timedelta(minutes=1))


class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _TtyOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _InterruptingTty(_TtyInput):
    def readline(self, _size: int = -1) -> str:
        raise KeyboardInterrupt


def _run_cli_with_tty(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    args: list[str],
) -> tuple[int, str, str]:
    stdin = _TtyInput(response)
    stdout = _TtyOutput()
    stderr = _TtyOutput()
    monkeypatch.setattr(session_module.sys, "stdin", stdin)
    monkeypatch.setattr(session_module.sys, "stdout", stdout)
    monkeypatch.setattr(session_module.sys, "stderr", stderr)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    status = session_module.main(args)
    return status, stdout.getvalue(), stderr.getvalue()


def _run_shortlist(
    root: Path,
    accounts: LocalAccountStore,
    alias: str,
    *,
    inbound_owner: str | None = None,
    inbound_cap: int | None = None,
    limit: int = 5,
) -> NurtureShortlistResult:
    session = session_module.RecruitmentSession(root, accounts)
    return asyncio.run(
        session.shortlist(
            alias,
            inbound_owner_username=inbound_owner,
            inbound_thread_cap=inbound_cap,
            limit=limit,
            now=_NOW,
        )
    )


def test_shortlist_uses_existing_ranking_and_dedupes_api_source_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _thread("synthetic-a", "career question? prepare for an interview")
    second = _thread("synthetic-b", "career opportunity")
    third = _thread("synthetic-c", "career planning")
    ordered_results: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    for index, mentions in enumerate(((first, second), (second, first))):
        root, accounts, _ = _account_root(tmp_path / f"run-{index}")
        api = _FakeApi(mentions=mentions, search=(first, third))
        _install_fake_app(monkeypatch, api)
        result = _run_shortlist(root, accounts, "alice")
        fingerprints = tuple(item.fingerprint for item in result.candidates)
        sources = tuple(str(item.source) for item in result.candidates)
        ordered_results.append((fingerprints, sources))

    assert ordered_results[0] == ordered_results[1]
    assert ordered_results[0][0].count(fingerprint_remote_thread("synthetic-a")) == 1
    assert ordered_results[0][1][0] == "MENTIONS"


def test_shortlist_explains_policy_and_durable_target_states_without_actionability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, (account,) = _account_root(tmp_path)
    preset = get_nurture_preset("recruitment")
    candidates = (
        _thread("eligible", "Synthetic career question?"),
        _thread("cooldown", "Synthetic career cooldown candidate"),
        _thread("pending", "Synthetic career pending candidate"),
        _thread("confirmed", "Synthetic career confirmed candidate"),
        _thread("ambiguous", "Synthetic career ambiguous candidate"),
        _thread("excluded", "Synthetic crypto career promotion"),
    )
    _seed_target(root, account, preset, "cooldown", "NONE")
    _seed_target(root, account, preset, "pending", "PENDING")
    _seed_target(root, account, preset, "confirmed", "CONFIRMED")
    _seed_target(root, account, preset, "ambiguous", "AMBIGUOUS")
    _install_fake_app(monkeypatch, _FakeApi(mentions=candidates))

    result = _run_shortlist(root, accounts, "alice", limit=10)
    by_id = {item.fingerprint: item for item in result.candidates}
    expected = {
        "eligible": ("ELIGIBLE", "REVIEW_CANDIDATE"),
        "cooldown": ("COOLDOWN", "OBSERVE_ONLY"),
        "pending": ("PENDING", "OBSERVE_ONLY"),
        "confirmed": ("CONFIRMED", "OBSERVE_ONLY"),
        "ambiguous": ("AMBIGUOUS", "OBSERVE_ONLY"),
        "excluded": ("NOT_ELIGIBLE", "OBSERVE_ONLY"),
    }
    assert {
        remote_id: (
            by_id[fingerprint_remote_thread(remote_id)].status,
            by_id[fingerprint_remote_thread(remote_id)].action_hint,
        )
        for remote_id in expected
    } == expected
    assert "LOCAL_SEEN_COOLDOWN" in by_id[fingerprint_remote_thread("cooldown")].reason_codes
    assert "LOCAL_PENDING" in by_id[fingerprint_remote_thread("pending")].reason_codes
    assert "LOCAL_AMBIGUOUS" in by_id[fingerprint_remote_thread("ambiguous")].reason_codes
    assert "EXCLUDED_TERM" in by_id[fingerprint_remote_thread("excluded")].reason_codes


@pytest.mark.parametrize(
    ("identity_username", "profile_error", "conversation_error", "expected_calls"),
    [
        ("foreign_owner", None, None, ("identity",)),
        ("synthetic_owner", _FakeReadError("THREADS_RATE_LIMITED"), None, ("identity", "profile")),
        (
            "synthetic_owner",
            None,
            _FakeReadError("THREADS_TRANSPORT_FAILURE"),
            ("identity", "profile", "conversation"),
        ),
    ],
)
def test_configured_inbound_failures_stop_before_discovery_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_username: str,
    profile_error: Exception | None,
    conversation_error: Exception | None,
    expected_calls: tuple[str, ...],
) -> None:
    root, accounts, _ = _account_root(tmp_path)
    own_post = _thread("synthetic-own-post", "Synthetic owner post")
    api = _FakeApi(
        search=(_thread("fallback", "career fallback candidate"),),
        identity_username=identity_username,
        profile_error=profile_error,
        conversation_error=conversation_error,
        owned_posts=(own_post,),
        replies=(
            RemoteReply(
                "synthetic-reply",
                "Synthetic reply body",
                None,
                own_post.remote_thread_id,
                own_post.remote_thread_id,
                False,
            ),
        ),
    )
    _install_fake_app(monkeypatch, api)

    with pytest.raises(session_module.RecruitmentSessionError) as error:
        _run_shortlist(
            root,
            accounts,
            "alice",
            inbound_owner="synthetic_owner",
            inbound_cap=2,
        )

    assert error.value.code in {"INBOUND_OWNERSHIP_MISMATCH", "INBOUND_READ_FAILED"}
    assert tuple(api.calls) == expected_calls
    assert "fallback" not in repr(error.value)
    assert _TOKEN_MARKER not in str(error.value)
    assert "private response detail" not in str(error.value)


def test_no_configured_inbound_keeps_legacy_discovery_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, _ = _account_root(tmp_path)
    api = _FakeApi(search=(_thread("legacy", "Synthetic career opportunity"),))
    _install_fake_app(monkeypatch, api)

    result = _run_shortlist(root, accounts, "alice")

    assert result.inbound_status.value == "INBOUND_NOT_CONFIGURED"
    assert "identity" not in api.calls
    assert "profile" not in api.calls
    assert "search" in api.calls
    assert result.candidates[0].fingerprint == fingerprint_remote_thread("legacy")


def test_shortlist_uses_account_scoped_state_and_writes_no_candidate_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, (alice, bob) = _account_root(tmp_path, ("alice", "bob"))
    preset = get_nurture_preset("recruitment")
    _seed_target(root, alice, preset, "same-remote-id", "PENDING")
    api = _FakeApi(mentions=(_thread("same-remote-id", "Synthetic career opening"),))
    _install_fake_app(monkeypatch, api)
    session = session_module.RecruitmentSession(root, accounts)

    alice_result, bob_result = asyncio.run(_both_shortlists(session, "alice", "bob"))

    assert alice_result.candidates[0].status == "PENDING"
    assert bob_result.candidates[0].status == "ELIGIBLE"
    assert alice.id != bob.id
    assert not (root / "nurture" / "runs" / str(bob.id)).exists()
    assert not (root / "nurture" / "state" / str(bob.id)).exists()
    assert not (root / "nurture" / "drafts").exists()
    assert not (root / "nurture" / "approvals").exists()
    assert (root / "nurture" / "locks" / f"{alice.id}.lock").exists()
    assert (root / "nurture" / "locks" / f"{bob.id}.lock").exists()
    persisted = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    assert b"Synthetic career opening" not in persisted
    assert b"same-remote-id" not in persisted


async def _both_shortlists(
    session: session_module.RecruitmentSession,
    first: str,
    second: str,
) -> tuple[NurtureShortlistResult, NurtureShortlistResult]:
    return await asyncio.gather(
        session.shortlist(first, now=_NOW), session.shortlist(second, now=_NOW)
    )


def test_shortlist_bounds_items_and_preview_and_hides_private_fields_from_repr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, _ = _account_root(tmp_path)
    threads = tuple(
        _thread(f"bounded-{index:02}", f"{_SECRET_MARKER} career {index} " + "x" * 900)
        for index in range(12)
    )
    _install_fake_app(monkeypatch, _FakeApi(mentions=threads))

    result = _run_shortlist(root, accounts, "alice", limit=10)

    assert len(result.candidates) == 10
    assert all(len(item.text or "") <= 500 for item in result.candidates)
    assert all(item.text_truncated for item in result.candidates)
    assert _SECRET_MARKER not in repr(result)
    assert _SECRET_MARKER not in repr(result.candidates)
    assert all(
        _REMOTE_USERNAME not in repr(item) and _REMOTE_URL not in repr(item)
        for item in result.candidates
    )


def test_cli_shortlist_requires_private_tty_and_selection_stays_preview_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, _ = _account_root(tmp_path)
    accounts.get("alice")
    api = _FakeApi(
        mentions=(
            _thread("private-first", "FIRST_PRIVATE_SENTINEL career?"),
            _thread("private-second", "SECOND_PRIVATE_SENTINEL career"),
        )
    )
    mutation_modes = _install_fake_app(monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    status, stdout, stderr = _run_cli_with_tty(
        monkeypatch,
        "2\n",
        ["--account", "alice", "--shortlist"],
    )

    assert status == 0
    assert stdout == ""
    assert "RECRUITMENT SHORTLIST — PRIVATE LOCAL REVIEW" in stderr
    assert "FIRST_PRIVATE_SENTINEL" not in stderr
    assert "SECOND_PRIVATE_SENTINEL" in stderr
    assert "Suggested next step: REVIEW_CANDIDATE" in stderr
    assert "No reply draft or remote action is available" in stderr
    assert (
        "private-first" not in stderr
        and _REMOTE_USERNAME not in stderr
        and _REMOTE_URL not in stderr
    )
    assert mutation_modes == [False]


def test_redirected_output_or_ci_rejects_shortlist_before_api_or_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _accounts, _ = _account_root(tmp_path)
    api = _FakeApi(mentions=(_thread("private", "PRIVATE career"),))
    _install_fake_app(monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(session_module.sys, "stdin", _TtyInput("1\n"))
    monkeypatch.setattr(session_module.sys, "stdout", io.StringIO())
    private_error = _TtyOutput()
    monkeypatch.setattr(session_module.sys, "stderr", private_error)

    status = session_module.main(["--account", "alice", "--shortlist"])

    assert status == 1
    assert private_error.getvalue() == "ERROR PRIVATE_REVIEW_TERMINAL_REQUIRED\n"
    assert api.calls == []
    assert not (root / "nurture").exists()


@pytest.mark.parametrize(
    ("response", "interrupt", "expected_status", "expected_message"),
    [
        ("", False, 0, "Review ended without a preview."),
        ("99\n", False, 0, "Selection outside the shortlist; review ended safely."),
        ("x\n", False, 0, "Invalid selection; review ended safely."),
        ("", True, 130, None),
    ],
)
def test_eof_invalid_input_and_ctrl_c_exit_without_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    interrupt: bool,
    expected_status: int,
    expected_message: str | None,
) -> None:
    root, _accounts, _ = _account_root(tmp_path)
    api = _FakeApi(mentions=(_thread("choice", "PRIVATE_SELECTION_SENTINEL career"),))
    _install_fake_app(monkeypatch, api)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    stdin = _InterruptingTty() if interrupt else _TtyInput(response)
    stdout = _TtyOutput()
    stderr = _TtyOutput()
    monkeypatch.setattr(session_module.sys, "stdin", stdin)
    monkeypatch.setattr(session_module.sys, "stdout", stdout)
    monkeypatch.setattr(session_module.sys, "stderr", stderr)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    status = session_module.main(["--account", "alice", "--shortlist"])

    assert status == expected_status
    assert stdout.getvalue() == ""
    if expected_message is not None:
        assert expected_message in stderr.getvalue()
    assert "PRIVATE_SELECTION_SENTINEL" not in stderr.getvalue()


def test_inbound_configuration_needs_owner_and_cap_and_no_config_does_not_claim_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, accounts, _ = _account_root(tmp_path)
    _install_fake_app(monkeypatch, _FakeApi(search=(_thread("search", "career role"),)))
    session = session_module.RecruitmentSession(root, accounts)

    with pytest.raises(session_module.RecruitmentSessionError) as error:
        asyncio.run(session.shortlist("alice", inbound_owner_username="synthetic_owner", now=_NOW))

    assert error.value.code == "INBOUND_CONFIGURATION_INVALID"
    result = asyncio.run(session.shortlist("alice", now=_NOW))
    assert result.inbound_status.value == "INBOUND_NOT_CONFIGURED"


def test_demo_source_script_runs_one_command_with_fake_data_only(tmp_path: Path) -> None:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT")
        if key in os.environ
    }
    environment["TEMP"] = str(tmp_path)
    environment["TMP"] = str(tmp_path)
    environment["TMPDIR"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--select", "2"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RECRUITMENT SHORTLIST — SYNTHETIC DEMO" in completed.stdout
    assert "Status: ELIGIBLE" in completed.stdout
    assert "Status: COOLDOWN" in completed.stdout
    assert "Status: NOT_ELIGIBLE" in completed.stdout
    assert "Synthetic selection: 2" in completed.stdout
    assert "Preview only; no approval or remote action was created." in completed.stdout
    assert "remote_mutations=0" in completed.stdout
    assert "SYNTHETIC_TOKEN_SENTINEL" not in completed.stdout + completed.stderr
    assert "example.invalid" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "conflicting_args",
    [
        ["--validate"],
        ["--preflight"],
        ["--observe"],
        ["--watch-observe"],
        ["--review"],
        ["--apply-reply-file", "synthetic-draft.json"],
        ["--apply-post-file", "synthetic-post.json"],
    ],
)
def test_shortlist_mode_is_mutually_exclusive_with_existing_modes(
    conflicting_args: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        session_module._build_parser().parse_args(
            ["--account", "alice", "--shortlist", *conflicting_args]
        )
    assert error.value.code == 2
