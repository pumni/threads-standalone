from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from threads_platform.browser.contracts import (
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserProcessCrashed,
)
from threads_platform.browser.threads_job_ui_reader import (
    UiBodyVisibility,
    UiFeedBatch,
    UiPageState,
    UiPostDetail,
    UiPostTarget,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.browser_nurture import (
    BrowserNurtureRunner,
    BrowserNurtureStatus,
    SyntheticBrowserNurtureFixtureV1,
)
from threads_platform.standalone.browser_nurture_state import (
    BrowserNurtureCheckpointV1,
    BrowserNurtureStateStore,
    validate_browser_nurture_checkpoint,
    validate_browser_nurture_config,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

_ORIGIN = "https://threads.example.invalid"
_ACTOR = "synthetic.operator"
_ACCOUNT = "test-browser-nurture"
_TOPICS = ("design", "engineering")
_BODIES = {
    "001": "A synthetic design engineer opportunity is available.",
    "002": "A synthetic community update about gardens.",
    "003": "A synthetic engineering post with details hidden below.",
    "004": "A synthetic senior engineer is mentoring new makers.",
}


def test_browser_nurture_checkpoint_v1_contract_is_preserved() -> None:
    checkpoint = BrowserNurtureCheckpointV1(
        account_uuid=UUID("00000000-0000-4000-8000-000000000001"),
        completed_cycles=1,
        scanned=1,
        relevant=1,
        seen_fingerprints=("0" * 64,),
    )

    document = checkpoint.to_document()

    assert set(document) == {
        "schema_version",
        "account_uuid",
        "completed_cycles",
        "scanned",
        "relevant",
        "irrelevant",
        "unknown",
        "duplicates",
        "scrolls",
        "seen_fingerprints",
        "crash_recovery_pending",
    }
    assert document["schema_version"] == 1
    assert validate_browser_nurture_checkpoint(document).to_document() == document


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _case_dirs(tmp_path: Path, name: str) -> tuple[Path, Path]:
    case = tmp_path / name
    _secure_directory(case)
    fixtures = case / "fixtures"
    _secure_directory(fixtures)
    root = case / "root"
    _secure_directory(root)
    return fixtures, root


def _threads_local_executable() -> str:
    executable = shutil.which("threads-local")
    if executable is None:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = Path(sys.executable).with_name(f"threads-local{suffix}")
        if candidate.is_file():
            executable = os.fspath(candidate)
    assert executable is not None, "run installed CLI acceptance under `uv run`"
    return executable


def _require_local_chromium() -> None:
    hosted = (
        os.environ.get("CI", "").casefold() == "true"
        or os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1"
    )
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            if hosted:
                pytest.fail("Playwright Chromium is required for the offline Nurture E2E")
            pytest.skip("Playwright Chromium is not installed")
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError:
            if hosted:
                pytest.fail("Playwright Chromium must launch for the offline Nurture E2E")
            pytest.skip("Playwright Chromium cannot launch in this environment")
        browser.close()


def _target(post_id: str) -> str:
    return f"{_ORIGIN}/@synthetic.author/post/{post_id}"


def _shell(actor: str, main: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<style>body{margin:0}main{display:block}.post-card{height:80px;"
        "border-bottom:1px solid #ddd}header[role=banner]{height:36px}</style></head><body>"
        f'<header role="banner"><a href="/@{actor}">Account</a></header>{main}'
        "</body></html>"
    )


def _card(post_id: str) -> str:
    target = _target(post_id)
    return (
        '<article class="post-card"><header>'
        '<a href="/@synthetic.author">@synthetic.author</a>'
        f'<a href="{target}">time</a>'
        f'</header><div dir="auto">Synthetic post {post_id}</div></article>'
    )


def _detail(post_id: str) -> str:
    target = _target(post_id)
    if post_id == "003":
        body = '<div dir="auto">A synthetic engineering post with details hidden below.<button>See more</button></div>'
    else:
        body = f'<div dir="auto">{_BODIES[post_id]}</div>'
    return _shell(
        _ACTOR,
        "<main><article><header>"
        '<a href="/@synthetic.author">@synthetic.author</a>'
        f'<a href="{target}">time</a>'
        f"</header>{body}</article></main>",
    )


def _fixture_document(
    *,
    actor: str = _ACTOR,
    scenario: str | None = None,
    topic_posts: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, object]:
    pages: list[dict[str, str]] = [
        {"url": f"{_ORIGIN}/", "html": _shell(actor, "<main><p>synthetic home</p></main>")}
    ]
    for topic in _TOPICS:
        query_url = f"{_ORIGIN}/search?{urlencode({'q': topic})}"
        post_ids = tuple(_BODIES) if topic_posts is None else topic_posts[topic]
        cards = "".join(_card(post_id) for post_id in post_ids)
        feed_html = _shell(
            _ACTOR,
            '<main><form role="search"><input type="search" value="'
            f"{topic}"
            '"></form>'
            f"{cards}<div style='height:1800px'></div></main>",
        )
        if scenario == "session-expired-during-search":
            feed_html = _shell(
                _ACTOR,
                '<main><form action="/login"><input type="password"></form></main>',
            )
        elif scenario == "selector-drift":
            feed_html = _shell(
                _ACTOR,
                '<main><form role="search"><input type="search" value="'
                f"{topic}"
                '"></form></main><main><section>changed layout</section></main>',
            )
        pages.append({"url": query_url, "html": feed_html})
    if scenario == "logged-out":
        pages[0]["html"] = _shell(
            _ACTOR,
            '<main><form action="/login"><input type="password"></form></main>',
        )
    elif scenario == "challenge":
        pages[0]["html"] = _shell(_ACTOR, '<main><iframe title="challenge"></iframe></main>')
    elif scenario == "redirect":
        pages[0]["html"] = (
            '<!doctype html><html><head><meta http-equiv="refresh" '
            'content="0;url=https://redirect.example.invalid/elsewhere"></head>'
            "<body><main></main></body></html>"
        )
    elif scenario == "wrong-account":
        pages[0]["html"] = _shell("synthetic-other", "<main><p>synthetic home</p></main>")
    for post_id in _BODIES:
        pages.append({"url": _target(post_id), "html": _detail(post_id)})
    return {
        "schema_version": 1,
        "fixture_kind": "SYNTHETIC_BROWSER_NURTURE",
        "pages": pages,
    }


def _write_fixture(fixtures: Path, value: object) -> Path:
    path = fixtures / "browser-nurture.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _run_cli(
    args: tuple[str, ...],
    *,
    cwd: Path,
    root: Path | None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("THREADS_LOCAL_DATA_ROOT", None)
    if root is not None:
        environment["THREADS_LOCAL_DATA_ROOT"] = os.fspath(root)
    return subprocess.run(
        [_threads_local_executable(), *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )


def _create_account_and_config(
    fixtures: Path,
    root: Path,
    *,
    max_posts_per_cycle: int = 10,
    relevance_terms: tuple[str, ...] = ("engineer",),
) -> LocalAccount:
    added = _run_cli(("account", "add", _ACCOUNT), cwd=fixtures, root=root)
    assert added.returncode == 0, added.stdout + added.stderr
    accounts = LocalAccountStore(root)
    account = accounts.get(_ACCOUNT)
    profile_directory = root / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        os.chmod(profile_directory, 0o700)
    config_args = [
        "browser-nurture",
        "configure",
        _ACCOUNT,
        "--expected-username",
        _ACTOR,
    ]
    for topic in _TOPICS:
        config_args.extend(("--topic", topic))
    for term in relevance_terms:
        config_args.extend(("--relevance-term", term))
    config_args.extend(("--max-posts-per-cycle", str(max_posts_per_cycle)))
    configured = _run_cli(
        tuple(config_args),
        cwd=fixtures,
        root=root,
    )
    assert configured.returncode == 0, configured.stdout + configured.stderr
    assert "BROWSER_NURTURE_CONFIG status=SAVED topics=2 relevance_terms=1" in configured.stdout
    return account


def test_installed_cli_runs_two_offline_cycles_and_resumes_checkpoint(tmp_path: Path) -> None:
    _require_local_chromium()
    fixtures, root = _case_dirs(tmp_path, "positive")
    account = _create_account_and_config(fixtures, root)
    fixture_path = _write_fixture(fixtures, _fixture_document())

    first = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "ui-fixture",
            "--fixture-file",
            os.fspath(fixture_path),
            "--data-root",
            os.fspath(root),
            "--continuous",
            "--max-cycles",
            "2",
            "--idle-seconds",
            "0",
        ),
        cwd=fixtures,
        root=root,
    )

    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stderr == ""
    assert "status=COMPLETE source=SYNTHETIC_FIXTURE" in first.stdout
    assert "cycles=2" in first.stdout
    assert "scanned=4 relevant=2 irrelevant=1 unknown=1" in first.stdout
    assert "duplicates=12 scrolls=12 sessions_opened=1 checkpoint_cycles=2" in first.stdout
    assert all(
        secret not in first.stdout + first.stderr
        for secret in (_ACCOUNT, _ACTOR, _BODIES["001"], _target("001"), _ORIGIN)
    )

    checkpoint_path = root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes.decode("utf-8"))
    assert checkpoint["completed_cycles"] == 2
    assert len(checkpoint["seen_fingerprints"]) == 4
    assert all(re.fullmatch(r"[a-f0-9]{64}", item) for item in checkpoint["seen_fingerprints"])
    assert all(
        secret not in checkpoint_bytes.decode("utf-8")
        for secret in (_target("001"), _BODIES["001"], _ACTOR)
    )

    replay = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "ui-fixture",
            "--fixture-file",
            os.fspath(fixture_path),
            "--data-root",
            os.fspath(root),
            "--max-cycles",
            "1",
            "--idle-seconds",
            "0",
        ),
        cwd=fixtures,
        root=root,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "cycles=1" in replay.stdout
    assert "scanned=0" in replay.stdout
    assert "checkpoint_cycles=3" in replay.stdout


def test_installed_cli_rotates_topic_selection_when_cycle_cap_is_one(tmp_path: Path) -> None:
    _require_local_chromium()
    fixtures, root = _case_dirs(tmp_path, "topic-rotation")
    account = _create_account_and_config(fixtures, root, max_posts_per_cycle=1)
    fixture_path = _write_fixture(
        fixtures,
        _fixture_document(topic_posts={"design": ("001",), "engineering": ("002",)}),
    )

    result = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "ui-fixture",
            "--fixture-file",
            os.fspath(fixture_path),
            "--data-root",
            os.fspath(root),
            "--continuous",
            "--max-cycles",
            "2",
            "--idle-seconds",
            "0",
        ),
        cwd=fixtures,
        root=root,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "cycles=2" in result.stdout
    assert "scanned=2 relevant=1 irrelevant=1 unknown=0" in result.stdout
    checkpoint_path = root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["next_topic_index"] == 0
    assert len(checkpoint["seen_fingerprints"]) == 2


def test_installed_cli_starts_new_policy_epoch_after_configuration_change(tmp_path: Path) -> None:
    _require_local_chromium()
    fixtures, root = _case_dirs(tmp_path, "policy-change")
    account = _create_account_and_config(fixtures, root, max_posts_per_cycle=1)
    fixture_path = _write_fixture(fixtures, _fixture_document())
    run_args = (
        "browser-nurture",
        "run",
        _ACCOUNT,
        "--source",
        "ui-fixture",
        "--fixture-file",
        os.fspath(fixture_path),
        "--data-root",
        os.fspath(root),
        "--max-cycles",
        "1",
        "--idle-seconds",
        "0",
    )

    first = _run_cli(run_args, cwd=fixtures, root=root)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "scanned=1 relevant=1 irrelevant=0 unknown=0" in first.stdout

    reconfigured = _run_cli(
        (
            "browser-nurture",
            "configure",
            _ACCOUNT,
            "--expected-username",
            _ACTOR,
            "--topic",
            _TOPICS[1],
            "--topic",
            _TOPICS[0],
            "--relevance-term",
            "garden",
            "--max-posts-per-cycle",
            "1",
        ),
        cwd=fixtures,
        root=root,
    )
    assert reconfigured.returncode == 0, reconfigured.stdout + reconfigured.stderr

    second = _run_cli(run_args, cwd=fixtures, root=root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "scanned=1 relevant=0 irrelevant=1 unknown=0" in second.stdout
    assert "duplicates=1" in second.stdout
    assert "checkpoint_cycles=1" in second.stdout

    checkpoint_path = root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["completed_cycles"] == 1
    assert checkpoint["scanned"] == 1
    assert checkpoint["relevant"] == 0
    assert checkpoint["irrelevant"] == 1
    assert len(checkpoint["seen_fingerprints"]) == 1


@pytest.mark.parametrize(
    ("scenario", "error_code"),
    (
        ("wrong-account", "BROWSER_SESSION_MISMATCH"),
        ("logged-out", "SESSION_EXPIRED"),
        ("challenge", "CHALLENGE_REQUIRED"),
        ("selector-drift", "BROWSER_SELECTOR_DRIFT"),
        ("session-expired-during-search", "SESSION_EXPIRED"),
        ("redirect", "REMOTE_STATE_UNCERTAIN"),
    ),
)
def test_installed_cli_stops_before_committing_unsafe_ui_state(
    tmp_path: Path,
    scenario: str,
    error_code: str,
) -> None:
    _require_local_chromium()
    fixtures, root = _case_dirs(tmp_path, scenario)
    account = _create_account_and_config(fixtures, root)
    fixture_path = _write_fixture(fixtures, _fixture_document(scenario=scenario))

    result = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "ui-fixture",
            "--fixture-file",
            os.fspath(fixture_path),
            "--data-root",
            os.fspath(root),
        ),
        cwd=fixtures,
        root=root,
    )

    assert result.returncode == 1
    assert f"ERROR {error_code}" in result.stderr
    assert "threads.example.invalid" not in result.stdout + result.stderr
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


def test_browser_source_gate_and_installed_cli_help_are_explicit(tmp_path: Path) -> None:
    missing_root = tmp_path / "must-not-be-created"
    disabled = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "browser",
            "--data-root",
            os.fspath(missing_root),
        ),
        cwd=tmp_path,
        root=None,
    )
    assert disabled.returncode == 1
    assert "status=DISABLED_PERMISSION_PENDING" in disabled.stdout
    assert not missing_root.exists()

    help_result = _run_cli(("browser-nurture", "run", "--help"), cwd=tmp_path, root=None)
    assert help_result.returncode == 0
    assert "--continuous" in help_result.stdout
    assert "--source {browser,ui-fixture}" in help_result.stdout


class _FakeNurtureSession:
    def __init__(
        self,
        *,
        crash_on_detail: bool,
        crash_on_search_calls: frozenset[int],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.crash_on_detail = crash_on_detail
        self.crash_on_search_calls = crash_on_search_calls
        self.search_calls = 0
        self.stop_event = stop_event
        self.closed = False
        self.ui_scroll_count = 1
        self._crashed = False
        self._target = UiPostTarget(
            "https://threads.example.invalid/@synthetic.author/post/001",
            "synthetic.author",
        )

    async def install_synthetic_fixture_pages(self, pages: object, *, origin: str) -> None:
        assert origin == _ORIGIN

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        assert url == f"{_ORIGIN}/"
        assert allowed_origins == frozenset({_ORIGIN})

    async def verify_ui_account(self, expected_actor: str, *, origin: str) -> UiPageState:
        assert expected_actor == _ACTOR
        assert origin == _ORIGIN
        return UiPageState.READY

    async def search_ui_feed(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> tuple[UiFeedBatch, ...]:
        assert query in _TOPICS
        self.search_calls += 1
        if self.search_calls in self.crash_on_search_calls:
            raise BrowserProcessCrashed()
        return (
            UiFeedBatch(
                batch_index=0,
                page_url=f"{_ORIGIN}/search?q={query}",
                state=UiPageState.READY,
                cards=(self._target,),
                has_more=False,
            ),
        )

    async def read_exact_ui_post(
        self,
        target: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> UiPostDetail:
        if self.crash_on_detail and not self._crashed:
            self._crashed = True
            raise BrowserProcessCrashed()
        if self.stop_event is not None:
            self.stop_event.set()
        return UiPostDetail(
            requested_url=target,
            page_url=target,
            canonical_post_url=target,
            state=UiPageState.READY,
            author_username="synthetic.author",
            body="A synthetic engineer position.",
            body_visibility=UiBodyVisibility.FULL_VISIBLE,
        )

    async def close(self) -> None:
        self.closed = True


class _FakeNurtureEngine:
    def __init__(
        self,
        crash_on_opens: frozenset[int],
        *,
        crash_on_search_calls: frozenset[int] = frozenset(),
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.crash_on_opens = crash_on_opens
        self.crash_on_search_calls = crash_on_search_calls
        self.stop_event = stop_event
        self.sessions: list[_FakeNurtureSession] = []

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        index = len(self.sessions) + 1
        session = _FakeNurtureSession(
            crash_on_detail=index in self.crash_on_opens,
            crash_on_search_calls=self.crash_on_search_calls,
            stop_event=self.stop_event,
        )
        self.sessions.append(session)
        return cast(BrowserEngineSession, session)


def _runner_case(
    tmp_path: Path, engine: _FakeNurtureEngine
) -> tuple[
    LocalAccount, BrowserNurtureRunner, BrowserNurtureStateStore, SyntheticBrowserNurtureFixtureV1
]:
    root = tmp_path / "runner-root"
    _secure_directory(root)
    accounts = LocalAccountStore(root)
    account = accounts.add("test-runner-nurture")
    profile = root / "profiles" / str(account.id)
    profile.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        os.chmod(profile, 0o700)
    state = BrowserNurtureStateStore(root)
    state.save_config(
        validate_browser_nurture_config(
            {
                "schema_version": 1,
                "account_uuid": str(account.id),
                "expected_username": _ACTOR,
                "topics": list(_TOPICS),
                "relevance_terms": ["engineer"],
                "max_posts_per_cycle": 5,
                "idle_seconds": 60,
            }
        )
    )
    pages = {
        f"{_ORIGIN}/": "synthetic",
        **{f"{_ORIGIN}/search?q={topic}": "synthetic" for topic in _TOPICS},
    }
    fixture = SyntheticBrowserNurtureFixtureV1(pages=pages)
    runner = BrowserNurtureRunner(LocalRuntime(root, accounts, engine), state)
    return account, runner, state, fixture


def test_runner_leaves_v1_checkpoint_untouched_when_starting_v2_state(tmp_path: Path) -> None:
    engine = _FakeNurtureEngine(frozenset())
    account, runner, state, fixture = _runner_case(tmp_path, engine)
    legacy_directory = tmp_path / "runner-root" / "browser_nurture_v1" / "checkpoints"
    legacy_directory.mkdir(mode=0o700)
    legacy_path = legacy_directory / f"{account.id}.json"
    legacy = BrowserNurtureCheckpointV1(
        account_uuid=account.id,
        completed_cycles=7,
        scanned=11,
        relevant=5,
        seen_fingerprints=("f" * 64,),
    )
    legacy_bytes = (
        json.dumps(
            legacy.to_document(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    legacy_path.write_bytes(legacy_bytes)

    result = asyncio.run(runner.run(account.alias, fixture, max_cycles=1))

    assert result.scanned == 1
    assert result.checkpoint_cycles == 1
    assert legacy_path.read_bytes() == legacy_bytes
    v2_checkpoint = state.load_checkpoint(account.id)
    assert v2_checkpoint.schema_version == 2
    assert v2_checkpoint.scanned == 1
    assert v2_checkpoint.completed_cycles == 1


def test_runner_restarts_once_after_browser_crash_without_committing_partial_cycle(
    tmp_path: Path,
) -> None:
    engine = _FakeNurtureEngine(frozenset({1}))
    account, runner, state, fixture = _runner_case(tmp_path, engine)

    result = asyncio.run(runner.run(account.alias, fixture, max_cycles=1))

    checkpoint = state.load_checkpoint(account.id)
    assert result.status is BrowserNurtureStatus.COMPLETE
    assert result.sessions_opened == 2
    assert result.scanned == 1
    assert checkpoint.completed_cycles == 1
    assert checkpoint.relevant == 1
    assert checkpoint.crash_recovery_pending is False
    assert engine.sessions[0].closed


def test_runner_stops_after_second_crash_and_keeps_committed_checkpoint(tmp_path: Path) -> None:
    engine = _FakeNurtureEngine(frozenset({1, 2}))
    account, runner, state, fixture = _runner_case(tmp_path, engine)

    result = asyncio.run(runner.run(account.alias, fixture, max_cycles=1))

    checkpoint = state.load_checkpoint(account.id)
    assert result.status is BrowserNurtureStatus.FAILED
    assert result.reason_code == "CRASH_RECOVERY_EXHAUSTED"
    assert result.sessions_opened == 2
    assert checkpoint.completed_cycles == 0
    assert checkpoint.crash_recovery_pending is True
    assert engine.sessions[0].closed
    assert engine.sessions[1].closed


def test_runner_limits_recovery_to_one_restart_across_multiple_cycles(tmp_path: Path) -> None:
    engine = _FakeNurtureEngine(
        frozenset({1}),
        crash_on_search_calls=frozenset({3}),
    )
    account, runner, state, fixture = _runner_case(tmp_path, engine)

    result = asyncio.run(runner.run(account.alias, fixture, max_cycles=3, idle_seconds=0))

    checkpoint = state.load_checkpoint(account.id)
    assert result.status is BrowserNurtureStatus.FAILED
    assert result.reason_code == "CRASH_RECOVERY_EXHAUSTED"
    assert result.sessions_opened == 2
    assert checkpoint.completed_cycles == 1
    assert checkpoint.crash_recovery_pending is True


def test_continuous_runner_stops_at_committed_cycle_and_resumes_without_reprocessing(
    tmp_path: Path,
) -> None:
    stop_event = asyncio.Event()
    engine = _FakeNurtureEngine(frozenset(), stop_event=stop_event)
    account, runner, state, fixture = _runner_case(tmp_path, engine)

    stopped = asyncio.run(
        runner.run(
            account.alias,
            fixture,
            max_cycles=None,
            continuous=True,
            idle_seconds=60,
            stop_event=stop_event,
        )
    )
    checkpoint_after_stop = state.load_checkpoint(account.id)
    stop_event.clear()
    resumed = asyncio.run(runner.run(account.alias, fixture, max_cycles=1))

    checkpoint_after_resume = state.load_checkpoint(account.id)
    assert stopped.status is BrowserNurtureStatus.STOPPED
    assert stopped.reason_code == "STOP_REQUESTED"
    assert checkpoint_after_stop.completed_cycles == 1
    assert resumed.status is BrowserNurtureStatus.COMPLETE
    assert resumed.scanned == 0
    assert checkpoint_after_resume.completed_cycles == 2


def test_long_lived_profile_lease_excludes_existing_one_shot_browser_commands(
    tmp_path: Path,
) -> None:
    engine = _FakeNurtureEngine(frozenset())
    root = tmp_path / "lock-root"
    _secure_directory(root)
    accounts = LocalAccountStore(root)
    account = accounts.add("test-lock-nurture")
    profile = root / "profiles" / str(account.id)
    profile.mkdir(parents=True, mode=0o700)
    runtime = LocalRuntime(root, accounts, engine)

    async def exercise() -> None:
        async with runtime.own_browser_profile(account.alias) as lease:
            assert lease.account.id == account.id
            with pytest.raises(StandaloneRuntimeError) as caught:
                await runtime.open_profile(account.alias, "synthetic_operator")
            assert caught.value.code == "ACCOUNT_BUSY"

    asyncio.run(exercise())
