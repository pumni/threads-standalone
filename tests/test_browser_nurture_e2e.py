from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from threads_platform.browser import playwright_engine as playwright_engine_module
from threads_platform.browser import threads_job_ui_reader as ui_reader_module
from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
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
from threads_platform.standalone import browser_nurture as browser_nurture_module
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.browser_nurture import (
    BrowserNurtureError,
    BrowserNurtureRunner,
    BrowserNurtureSource,
    BrowserNurtureStatus,
    SyntheticBrowserNurtureFixtureV1,
)
from threads_platform.standalone.browser_nurture_state import (
    BrowserNurtureCheckpointV1,
    BrowserNurtureStateStore,
    browser_nurture_policy_fingerprint,
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


def test_continuous_opt_in_is_versioned_account_and_policy_scoped(tmp_path: Path) -> None:
    root = tmp_path / "continuous-opt-in-root"
    _secure_directory(root)
    account_id = UUID("00000000-0000-4000-8000-000000000002")
    config = validate_browser_nurture_config(
        {
            "schema_version": 1,
            "account_uuid": str(account_id),
            "expected_username": "offline.operator",
            "topics": ["engineering"],
            "relevance_terms": ["engineer"],
            "max_posts_per_cycle": 10,
            "idle_seconds": 60,
        }
    )
    state = BrowserNurtureStateStore(root)
    fingerprint = browser_nurture_policy_fingerprint(config)

    assert not state.continuous_opt_in_enabled(
        account_id,
        policy_fingerprint=fingerprint,
    )
    state.set_continuous_opt_in(
        account_id,
        policy_fingerprint=fingerprint,
        enabled=True,
    )
    path = root / "browser_nurture_v2" / "continuous_opt_in" / f"{account_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "account_uuid",
        "policy_fingerprint",
        "continuous_enabled",
    }
    assert document["schema_version"] == 1
    assert state.continuous_opt_in_enabled(account_id, policy_fingerprint=fingerprint)
    assert not state.continuous_opt_in_enabled(account_id, policy_fingerprint="0" * 64)
    assert not state.continuous_opt_in_enabled(
        UUID("00000000-0000-4000-8000-000000000003"),
        policy_fingerprint=fingerprint,
    )

    state.disable_continuous_opt_in(account_id)
    assert not state.continuous_opt_in_enabled(account_id, policy_fingerprint=fingerprint)


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
        f'<header role="banner"><a href="/@{actor}" aria-label="Profile">Profile</a></header>{main}'
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
    return _detail_for_actor(post_id, _ACTOR)


def _detail_for_actor(post_id: str, actor: str) -> str:
    target = _target(post_id)
    if post_id == "003":
        body = '<div dir="auto">A synthetic engineering post with details hidden below.<button>See more</button></div>'
    else:
        body = f'<div dir="auto">{_BODIES[post_id]}</div>'
    return _shell(
        actor,
        "<main><article><header>"
        '<a href="/@synthetic.author">@synthetic.author</a>'
        f'<a href="{target}">time</a>'
        f"</header>{body}</article></main>",
    )


def _two_area_home(actor: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<style>body{margin:0;font:16px sans-serif}[role=navigation]{width:220px;float:left}"
        "[role=navigation] a{display:block;padding:12px}main{margin-left:240px}</style></head><body>"
        '<div role="navigation">'
        '<a href="/" aria-label="For you">For you</a>'
        '<a href="/search" aria-label="Search">Search</a>'
        f'<a href="/@{actor}" aria-label="Profile">Profile</a>'
        "</div><section><h1>For you</h1>"
        '<div role="textbox" contenteditable="true" aria-label="Start a thread"></div>'
        "</section></body></html>"
    )


def _with_two_area_navigation(document: str, actor: str) -> str:
    navigation = (
        '<div role="navigation"><a href="/" aria-label="For you">For you</a>'
        '<a href="/search" aria-label="Search">Search</a>'
        f'<a href="/@{actor}" aria-label="Profile">Profile</a></div>'
    )
    return document.replace("<body>", f"<body>{navigation}", 1)


def _visual_card(post_id: str) -> str:
    target = _target(post_id).replace(_ORIGIN, "")
    body = _BODIES[post_id]
    expansion = ""
    if post_id == "003":
        body = "A synthetic engineering post with details hidden below."
        expansion = "<button>See more</button>"
    return (
        '<div class="visual-post-card" style="min-height:320px">'
        "<header>"
        '<a href="/@synthetic.author">@synthetic.author</a>'
        f'<a href="{target}">time</a>'
        f'</header><div dir="auto">{body}{expansion}</div></div>'
    )


def _search_panel(actor: str, cards: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<style>body{margin:0;font:16px sans-serif}[role=navigation]{width:220px;float:left}"
        "aside{margin-left:720px;width:400px;height:420px;overflow-y:auto;border-left:1px solid #ddd}"
        ".visual-post-card{border-bottom:1px solid #ddd;padding:8px}</style></head><body>"
        '<div role="navigation">'
        '<a href="/" aria-label="For you">For you</a>'
        '<a href="/search" aria-label="Search">Search</a>'
        f'<a href="/@{actor}" aria-label="Profile">Profile</a>'
        "</div><section><h1>For you</h1>"
        '<article><a href="/@synthetic.author/post/home-only">Home-only post</a></article>'
        "</section>"
        '<aside role="complementary" aria-label="Search">'
        '<input type="search" role="searchbox" aria-label="Search">'
        '<div role="tablist"><button role="tab" aria-selected="true">Top</button>'
        '<button role="tab" aria-selected="false">Recent</button></div>'
        f'<section aria-label="Search results">{cards}</section></aside></body></html>'
    )


def _visual_detail(actor: str, post_id: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<style>body{margin:0;font:16px sans-serif}[role=navigation]{width:220px;float:left}"
        "section{margin-left:240px}</style></head><body>"
        '<div role="navigation">'
        '<a href="/" aria-label="For you">For you</a>'
        '<a href="/search" aria-label="Search">Search</a>'
        f'<a href="/@{actor}" aria-label="Profile">Profile</a>'
        f'</div><section><div class="visual-post-card">{_visual_card(post_id)}</div></section>'
        "</body></html>"
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
            actor,
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
    elif scenario == "rate-limit":
        pages[0]["html"] = _shell(
            _ACTOR,
            '<main><div role="alert" aria-label="Rate limit">Synthetic wait</div></main>',
        )
    elif scenario == "redirect":
        pages[0]["html"] = (
            '<!doctype html><html><head><meta http-equiv="refresh" '
            'content="0;url=https://redirect.example.invalid/elsewhere"></head>'
            "<body><main></main></body></html>"
        )
    elif scenario == "wrong-account":
        pages[0]["html"] = _shell("synthetic-other", "<main><p>synthetic home</p></main>")
    for post_id in _BODIES:
        pages.append({"url": _target(post_id), "html": _detail_for_actor(post_id, actor)})
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
    assert "search_scanned=4 search_relevant=2 search_irrelevant=1 search_unknown=1" in first.stdout
    assert (
        "search_duplicates=12 search_scrolls=12 sessions_opened=1 checkpoint_cycles=2"
        in first.stdout
    )
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
    assert "search_scanned=0" in replay.stdout
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
    assert (
        "search_scanned=2 search_relevant=1 search_irrelevant=1 search_unknown=0" in result.stdout
    )
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
    assert "search_scanned=1 search_relevant=1 search_irrelevant=0 search_unknown=0" in first.stdout

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
    assert (
        "search_scanned=1 search_relevant=0 search_irrelevant=1 search_unknown=0" in second.stdout
    )
    assert "search_duplicates=1" in second.stdout
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
        ("rate-limit", "RATE_LIMITED"),
        ("selector-drift", "SEARCH_ROOT_UNRECOGNIZED"),
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


def test_browser_source_requires_explicit_read_only_selection_and_installed_cli_help(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "must-not-be-created"
    no_confirmation = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "browser",
        ),
        cwd=tmp_path,
        root=None,
    )
    assert no_confirmation.returncode == 1
    assert "ERROR LIVE_CONFIRMATION_REQUIRED" in no_confirmation.stderr
    assert not missing_root.exists()

    redirected_root = _run_cli(
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
    assert redirected_root.returncode == 1
    assert "ERROR LIVE_DATA_ROOT_NOT_ALLOWED" in redirected_root.stderr
    assert not missing_root.exists()

    unattended = _run_cli(
        (
            "browser-nurture",
            "run",
            _ACCOUNT,
            "--source",
            "browser",
            "--confirm-live-read-only",
        ),
        cwd=tmp_path,
        root=None,
    )
    assert unattended.returncode == 1
    assert "ERROR INTERACTIVE_CONSOLE_REQUIRED" in unattended.stderr
    assert not missing_root.exists()

    help_result = _run_cli(("browser-nurture", "run", "--help"), cwd=tmp_path, root=None)
    assert help_result.returncode == 0
    assert "--continuous" in help_result.stdout
    assert "--source {browser,ui-fixture}" in help_result.stdout
    assert "--confirm-live-read-only" in help_result.stdout
    command_help = _run_cli(("browser-nurture", "--help"), cwd=tmp_path, root=None)
    assert command_help.returncode == 0
    assert "enable-continuous" in command_help.stdout
    assert "disable-continuous" in command_help.stdout


def _offline_live_pages(actor: str) -> dict[str, str]:
    cards = "".join(_visual_card(post_id) for post_id in _BODIES)
    pages = {
        f"{BROWSER_FEED_ORIGIN}/": _two_area_home(actor),
        f"{BROWSER_FEED_ORIGIN}/search": _search_panel(actor, cards),
    }
    for post_id in _BODIES:
        target = _target(post_id).replace(_ORIGIN, BROWSER_FEED_ORIGIN)
        pages[target] = _visual_detail(actor, post_id)
    return pages


def _manual_assist_pages(actor: str, *, scoped_search: bool = True) -> dict[str, str]:
    target = _target("001").replace(_ORIGIN, "")
    card = (
        '<article><header><a href="/@synthetic.author">@synthetic.author</a>'
        f'<a id="selected-post-link" href="{target}">time</a></header>'
        f'<div dir="auto">{_BODIES["001"]}</div></article>'
    )
    root_open = (
        '<div role="dialog" aria-label="Search">'
        if scoped_search
        else '<div class="search-overlay">'
    )
    root_close = "</div>"
    search = (
        "<!doctype html><html><head><meta charset=utf-8></head><body>"
        '<nav role="navigation"><a href="/@'
        f'{actor}" aria-label="Profile">Profile</a></nav>'
        '<main><article><header><a href="/@home.author">@home.author</a>'
        '<a id="home-only-link" href="/@home.author/post/home-only">time</a></header>'
        "<div dir=auto>Home feed must stay outside Search results.</div></article></main>"
        f"{root_open}"
        '<input type="search" role="searchbox" aria-label="Search" value="design">'
        '<div role="tablist"><button role="tab" aria-selected="true">Top</button>'
        '<button role="tab" aria-selected="false">Recent</button></div>'
        f'<section role="region" aria-label="Search results">{card}</section>{root_close}'
        "</body></html>"
    )
    detail = (
        "<!doctype html><html><head><meta charset=utf-8></head><body>"
        f'<nav role="navigation"><a href="/@{actor}" aria-label="Profile">Profile</a></nav>'
        '<main><article><header><a href="/@synthetic.author">@synthetic.author</a>'
        f'<a href="{target}">time</a></header><div dir="auto">{_BODIES["001"]}</div>'
        "</article></main></body></html>"
    )
    other_detail = (
        "<!doctype html><html><head><meta charset=utf-8></head><body>"
        f'<nav role="navigation"><a href="/@{actor}" aria-label="Profile">Profile</a></nav>'
        '<main><article><header><a href="/@home.author">@home.author</a>'
        '<a href="/@home.author/post/home-only">time</a></header>'
        "<div dir=auto>Home-only synthetic content.</div></article></main></body></html>"
    )
    return {
        f"{BROWSER_FEED_ORIGIN}/": _two_area_home(actor),
        f"{BROWSER_FEED_ORIGIN}/search?q=design": search,
        f"{BROWSER_FEED_ORIGIN}{target}": detail,
        f"{BROWSER_FEED_ORIGIN}/@home.author/post/home-only": other_detail,
    }


def _install_offline_live_browser_transport(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, str],
    *,
    opened_sessions: list[Any] | None = None,
) -> list[str]:
    original_open = playwright_engine_module.PlaywrightBrowserEngine.open
    requested_urls: list[str] = []

    async def headless_chromium_for_test(engine: Any, request: BrowserLaunchRequest) -> Any:
        assert request.headless is False
        session = await original_open(engine, replace(request, headless=True))
        if opened_sessions is not None:
            opened_sessions.append(session)
        return session

    async def fulfill_offline_pages(_session: Any, route: Any) -> None:
        url = route.request.url
        requested_urls.append(url)
        html = pages.get(url)
        if html is None:
            await route.abort("blockedbyclient")
            return
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=html,
        )

    monkeypatch.setattr(
        playwright_engine_module.PlaywrightBrowserEngine,
        "open",
        headless_chromium_for_test,
    )
    monkeypatch.setattr(
        playwright_engine_module._PlaywrightBrowserSession,
        "_guard_navigation",
        fulfill_offline_pages,
    )
    return requested_urls


def _create_browser_source_account(root: Path, expected_actor: str) -> LocalAccount:
    accounts = LocalAccountStore(root)
    account = accounts.add("offline-browser-nurture")
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
                "expected_username": expected_actor,
                "topics": list(_TOPICS),
                "relevance_terms": ["engineer"],
                "max_posts_per_cycle": 10,
                "idle_seconds": 60,
            }
        )
    )
    return account


def _run_offline_browser_source_cli(
    root: Path,
    account: LocalAccount,
    pages: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, Any, list[str]]:
    requested_urls = _install_offline_live_browser_transport(monkeypatch, pages)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "run",
            account.alias,
            "--source",
            "browser",
            "--confirm-live-read-only",
            "--max-cycles",
            "1",
        )
    )
    return exit_code, capsys.readouterr(), requested_urls


def test_first_live_cli_cycle_rejects_multiple_cycles_before_browser_or_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "live-first-cycle-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    state = BrowserNurtureStateStore(root)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)

    def reject_if_runner_starts(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("multi-cycle one-shot reached the browser runner")

    monkeypatch.setattr(cli_module, "_run_browser_nurture_with_stop", reject_if_runner_starts)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "run",
            account.alias,
            "--source",
            "browser",
            "--confirm-live-read-only",
            "--max-cycles",
            "2",
        )
    )

    output = capsys.readouterr()
    config = state.load_config(account.id)
    assert config is not None
    checkpoint = state.load_checkpoint(
        account.id,
        policy_fingerprint=browser_nurture_policy_fingerprint(config),
    )
    assert exit_code == 1
    assert "ERROR RUN_POLICY_INVALID" in output.err
    assert checkpoint.completed_cycles == 0


def test_browser_source_runs_live_cli_path_through_offline_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "live-browser-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    requested_urls = _install_offline_live_browser_transport(monkeypatch, pages)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "run",
            account.alias,
            "--source",
            "browser",
            "--confirm-live-read-only",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert "search_scanned=4 search_relevant=2 search_irrelevant=1 search_unknown=1" in output.out
    assert any(url == f"{BROWSER_FEED_ORIGIN}/" for url in requested_urls)
    assert set(requested_urls).issubset(pages)
    assert requested_urls
    checkpoint = BrowserNurtureStateStore(root).load_checkpoint(account.id)
    assert checkpoint.completed_cycles == 1
    assert checkpoint.scanned == 4
    assert not BrowserNurtureStateStore(root).continuous_opt_in_enabled(
        account.id,
        policy_fingerprint=checkpoint.policy_fingerprint,
    )

    enable_code = cli_module.main(
        (
            "browser-nurture",
            "enable-continuous",
            account.alias,
            "--confirm-supervised-read-only",
        )
    )
    assert enable_code == 0
    assert "BROWSER_NURTURE_CONTINUOUS status=ENABLED" in capsys.readouterr().out

    continuous_code = cli_module.main(
        (
            "browser-nurture",
            "run",
            account.alias,
            "--source",
            "browser",
            "--continuous",
            "--max-cycles",
            "1",
        )
    )
    continuous_output = capsys.readouterr()
    assert continuous_code == 0, continuous_output.out + continuous_output.err
    assert "status=COMPLETE source=BROWSER_UI" in continuous_output.out
    assert "search_scanned=0" in continuous_output.out
    checkpoint = BrowserNurtureStateStore(root).load_checkpoint(account.id)
    assert checkpoint.completed_cycles == 2

    disable_code = cli_module.main(("browser-nurture", "disable-continuous", account.alias))
    assert disable_code == 0
    assert "BROWSER_NURTURE_CONTINUOUS status=DISABLED" in capsys.readouterr().out
    assert not BrowserNurtureStateStore(root).continuous_opt_in_enabled(
        account.id,
        policy_fingerprint=checkpoint.policy_fingerprint,
    )


def test_browser_source_scrolls_nested_search_results_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "nested-search-scroll-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    pages = _offline_live_pages("offline.operator")
    panel = _search_panel("offline.operator", "".join(_visual_card(post_id) for post_id in _BODIES))
    panel = panel.replace("height:420px;overflow-y:auto", "height:420px;overflow-y:hidden")
    panel = panel.replace(
        "</style>",
        'aside section[aria-label="Search results"]{height:380px;overflow-y:auto}</style>',
    )
    pages[f"{BROWSER_FEED_ORIGIN}/search"] = panel

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0, output.out + output.err
    assert "search_scanned=4" in output.out
    scroll_count = next(
        int(part.removeprefix("search_scrolls="))
        for part in output.out.split()
        if part.startswith("search_scrolls=")
    )
    assert scroll_count > 0
    assert set(requested_urls).issubset(pages)


def test_browser_source_stops_on_visible_actor_mismatch_before_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "live-browser-mismatch-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    pages = _offline_live_pages("offline.other")
    requested_urls = _install_offline_live_browser_transport(monkeypatch, pages)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "run",
            account.alias,
            "--source",
            "browser",
            "--confirm-live-read-only",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR BROWSER_SESSION_MISMATCH" in output.err
    assert requested_urls == [f"{BROWSER_FEED_ORIGIN}/"]
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


def test_browser_source_waits_for_delayed_home_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 1_000)
    root = tmp_path / "delayed-home-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    pages[f"{BROWSER_FEED_ORIGIN}/"] = (
        "<!doctype html><html><body><div id=app></div><script>setTimeout(() => {"
        "const nav = document.createElement('div'); nav.setAttribute('role', 'navigation');"
        f'nav.innerHTML = \'<a href="/" aria-label="For you">For you</a><a href="/search" aria-label="Search">Search</a><a href="/@{actor}" aria-label="Profile">Profile</a>\';'
        "document.body.append(nav);"
        "const section = document.createElement('section');"
        'section.innerHTML = \'<h1>For you</h1><div role="textbox" contenteditable="true"></div>\';'
        "document.body.append(section);"
        "}, 250);</script></body></html>"
    )

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert requested_urls[0] == f"{BROWSER_FEED_ORIGIN}/"
    assert requested_urls.count(f"{BROWSER_FEED_ORIGIN}/") >= 1
    assert set(requested_urls).issubset(pages)
    assert BrowserNurtureStateStore(root).load_checkpoint(account.id).completed_cycles == 1


def test_browser_source_accepts_explicit_self_profile_in_two_area_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "semantic-header-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    pages[f"{BROWSER_FEED_ORIGIN}/"] = _two_area_home(actor)

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert requested_urls[0] == f"{BROWSER_FEED_ORIGIN}/"
    assert set(requested_urls).issubset(pages)
    assert BrowserNurtureStateStore(root).load_checkpoint(account.id).completed_cycles == 1


@pytest.mark.parametrize("identity_case", ("arbitrary-profile-link", "duplicate-self-control"))
def test_browser_source_does_not_guess_or_accept_ambiguous_self_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    identity_case: str,
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 250)
    root = tmp_path / f"ambiguous-self-{identity_case}"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    home = _two_area_home(actor)
    if identity_case == "arbitrary-profile-link":
        home = home.replace(
            f'<a href="/@{actor}" aria-label="Profile">Profile</a>',
            f'<a href="/@{actor}">@{actor}</a>',
        )
    else:
        home = home.replace(
            f'<a href="/@{actor}" aria-label="Profile">Profile</a>',
            f'<a href="/@{actor}" aria-label="Profile">Profile</a>'
            f'<a href="/@{actor}" aria-label="Profile">Profile</a>',
        )
    pages[f"{BROWSER_FEED_ORIGIN}/"] = home

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert "ERROR HOME_ACTOR_UNVERIFIED" in output.err
    assert requested_urls == [f"{BROWSER_FEED_ORIGIN}/"]
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


@pytest.mark.parametrize(
    ("home_html", "reason"),
    (
        ("<!doctype html><html><body></body></html>", "HOME_UI_NOT_READY"),
        (
            (
                "<!doctype html><html><body><div style='width:120px;height:30px'>"
                "Synthetic unsupported shell</div></body></html>"
            ),
            "HOME_ROOT_UNRECOGNIZED",
        ),
        (
            "<!doctype html><html><body><main>Synthetic Home</main></body></html>",
            "HOME_ACTOR_UNVERIFIED",
        ),
        (
            (
                "<!doctype html><html><body><main>Synthetic Home</main>"
                '<header><a href="/@offline.operator" aria-label="Your profile">Profile</a></header>'
                "</body></html>"
            ),
            "HOME_ACTOR_UNVERIFIED",
        ),
        (
            (
                "<!doctype html><html><body>"
                '<header><a href="/@offline.operator">Profile</a></header>'
                "<section>Synthetic Home</section></body></html>"
            ),
            "HOME_ROOT_UNRECOGNIZED",
        ),
        (
            (
                "<!doctype html><html><body><main>Synthetic Home</main>"
                '<header role="banner"><a href="/@offline.operator">Profile</a>'
                '<a href="/@offline.other" aria-label="Profile">Profile</a></header></body></html>'
            ),
            "HOME_ACTOR_UNVERIFIED",
        ),
    ),
)
def test_browser_source_fails_closed_without_unique_home_actor_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home_html: str,
    reason: str,
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 250)
    root = tmp_path / "unverified-home-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    pages = _offline_live_pages("offline.operator")
    pages[f"{BROWSER_FEED_ORIGIN}/"] = home_html

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert f"ERROR {reason}" in output.err
    assert all(
        private_value not in output.out + output.err
        for private_value in (
            "offline.operator",
            "threads.com",
            "Synthetic Home",
            "design",
            "/@synthetic.author/post/001",
        )
    )
    assert requested_urls == [f"{BROWSER_FEED_ORIGIN}/"]
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


def test_browser_source_waits_for_delayed_search_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 1_000)
    root = tmp_path / "delayed-search-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    card = _visual_card("001")
    pages[f"{BROWSER_FEED_ORIGIN}/search"] = _search_panel(actor, "").replace(
        "</section></aside>",
        f"</section><script>setTimeout(() => document.querySelector('aside section').insertAdjacentHTML('beforeend', {json.dumps(card)}), 250);</script></aside>",
    )

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert f"{BROWSER_FEED_ORIGIN}/search" in requested_urls
    assert set(requested_urls).issubset(pages)
    assert BrowserNurtureStateStore(root).load_checkpoint(account.id).completed_cycles == 1


def test_browser_source_accepts_explicit_search_no_results_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "search-no-results-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    pages = _offline_live_pages("offline.operator")
    pages[f"{BROWSER_FEED_ORIGIN}/search"] = _search_panel(
        "offline.operator",
        '<div role="status">No results found</div>',
    )

    exit_code, output, _ = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert "search_scanned=0" in output.out
    checkpoint = BrowserNurtureStateStore(root).load_checkpoint(account.id)
    assert checkpoint.completed_cycles == 1
    assert checkpoint.scanned == 0


def test_browser_source_rejects_unrecognized_search_panel_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 250)
    root = tmp_path / "search-panel-drift-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    pages = _offline_live_pages("offline.operator")
    pages[f"{BROWSER_FEED_ORIGIN}/search"] = (
        "<!doctype html><html><body>"
        '<div role="navigation"><a href="/" aria-label="For you">For you</a>'
        '<a href="/search" aria-label="Search">Search</a>'
        '<a href="/@offline.operator" aria-label="Profile">Profile</a></div>'
        '<section><input type="search" role="searchbox"></section></body></html>'
    )

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert "ERROR SEARCH_ROOT_UNRECOGNIZED" in output.err
    assert requested_urls == [f"{BROWSER_FEED_ORIGIN}/", f"{BROWSER_FEED_ORIGIN}/search"]
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


@pytest.mark.parametrize(
    "variant", ("missing-permalink", "ambiguous-permalink", "missing-author", "ambiguous-author")
)
def test_browser_source_rejects_unbound_search_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    _require_local_chromium()
    root = tmp_path / f"search-card-{variant}-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    card = _card("001").replace(_ORIGIN, BROWSER_FEED_ORIGIN)
    target = _target("001").replace(_ORIGIN, BROWSER_FEED_ORIGIN)
    if variant == "missing-permalink":
        card = card.replace(f'<a href="{target}">time</a>', "<span>time</span>")
    elif variant == "ambiguous-permalink":
        card = card.replace(
            "</header>",
            '<a href="/@synthetic.author/post/002">another time</a></header>',
        )
    elif variant == "missing-author":
        card = card.replace(
            '<a href="/@synthetic.author">@synthetic.author</a>',
            "<span>@synthetic.author</span>",
        )
    else:
        card = card.replace(
            "</header>",
            '<a href="/@offline.other">Other author</a></header>',
        )
    pages[f"{BROWSER_FEED_ORIGIN}/search"] = _search_panel(actor, card)

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert "ERROR SEARCH_CARD_UNRECOGNIZED" in output.err
    assert not any("/post/" in url for url in requested_urls)
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


@pytest.mark.parametrize(
    "variant",
    ("missing-root", "missing-author", "mismatched-author", "ambiguous-author", "ambiguous-body"),
)
def test_browser_source_rejects_unproven_exact_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    _require_local_chromium()
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 250)
    root = tmp_path / f"detail-{variant}-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    target = _target("001").replace(_ORIGIN, BROWSER_FEED_ORIGIN)
    detail = _with_two_area_navigation(
        _detail_for_actor("001", actor).replace(_ORIGIN, BROWSER_FEED_ORIGIN), actor
    )
    reason = (
        "DETAIL_ROOT_UNRECOGNIZED" if variant == "missing-root" else "DETAIL_PROVENANCE_UNVERIFIED"
    )
    if variant == "missing-root":
        detail = _with_two_area_navigation(
            _shell(actor, "<main><p>Synthetic unrelated detail</p></main>").replace(
                _ORIGIN,
                BROWSER_FEED_ORIGIN,
            ),
            actor,
        )
    elif variant == "missing-author":
        detail = detail.replace(
            '<a href="/@synthetic.author">@synthetic.author</a>',
            "<span>synthetic.author</span>",
        )
    elif variant == "mismatched-author":
        detail = detail.replace(
            'href="/@synthetic.author">@synthetic.author',
            'href="/@offline.other">@offline.other',
            1,
        )
    elif variant == "ambiguous-author":
        detail = detail.replace(
            '<a href="/@synthetic.author">@synthetic.author</a>',
            '<a href="/@synthetic.author">@synthetic.author</a>'
            '<a href="/@offline.other">@offline.other</a>',
            1,
        )
    else:
        detail = detail.replace(
            "</article>",
            '<div dir="auto">Synthetic second body</div></article>',
        )
    pages[target] = detail

    exit_code, output, _ = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert f"ERROR {reason}" in output.err
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()


def test_browser_source_keeps_missing_visible_body_unknown_and_checkpoints_exact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "detail-missing-body-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    target = _target("001").replace(_ORIGIN, BROWSER_FEED_ORIGIN)
    detail = _with_two_area_navigation(
        _detail_for_actor("001", actor).replace(_ORIGIN, BROWSER_FEED_ORIGIN), actor
    )
    detail = detail.replace(
        '<div dir="auto">A synthetic design engineer opportunity is available.</div>',
        "",
    )
    pages[target] = detail

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    checkpoint = BrowserNurtureStateStore(root).load_checkpoint(account.id)
    assert exit_code == 0, output.out + output.err
    assert "status=COMPLETE source=BROWSER_UI" in output.out
    assert checkpoint.completed_cycles == 1
    assert checkpoint.unknown >= 1
    assert target in requested_urls


@pytest.mark.parametrize("failure", ("crash", "timeout"))
def test_browser_source_does_not_advance_checkpoint_when_cycle_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    _require_local_chromium()
    root = tmp_path / f"interrupted-cycle-{failure}-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _offline_live_pages(actor)
    if failure == "crash":

        async def crash_before_detail(self: Any, target: str, **kwargs: Any) -> Any:
            await self._context.close()
            raise BrowserProcessCrashed()

        monkeypatch.setattr(
            playwright_engine_module._PlaywrightBrowserSession,
            "read_exact_ui_post",
            crash_before_detail,
        )
    else:
        original_read = playwright_engine_module._PlaywrightBrowserSession.read_exact_ui_post

        async def timeout_before_detail(self: Any, target: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0.2)
            return await original_read(self, target, **kwargs)

        monkeypatch.setattr(
            playwright_engine_module._PlaywrightBrowserSession,
            "read_exact_ui_post",
            timeout_before_detail,
        )
        monkeypatch.setattr(browser_nurture_module, "MAX_NURTURE_CYCLE_SECONDS", 0.05)

    exit_code, output, requested_urls = _run_offline_browser_source_cli(
        root,
        account,
        pages,
        monkeypatch,
        capsys,
    )

    checkpoint = BrowserNurtureStateStore(root).load_checkpoint(account.id)
    assert exit_code == 1
    assert checkpoint.completed_cycles == 0
    assert set(requested_urls).issubset(pages)
    if failure == "crash":
        assert "CRASH_RECOVERY_EXHAUSTED" in output.out
        assert checkpoint.crash_recovery_pending is True
    else:
        assert "ERROR CYCLE_TIMEOUT" in output.err
        assert checkpoint.crash_recovery_pending is False


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


def test_runner_without_source_or_fixture_does_not_start_live_browser(tmp_path: Path) -> None:
    engine = _FakeNurtureEngine(frozenset())
    account, runner, _state, _fixture = _runner_case(tmp_path, engine)

    with pytest.raises(BrowserNurtureError) as error:
        asyncio.run(runner.run(account.alias))

    assert error.value.code == "RUN_POLICY_INVALID"
    assert engine.sessions == []


def test_live_runner_one_shot_rejects_multiple_cycles_before_browser_start(
    tmp_path: Path,
) -> None:
    root = tmp_path / "live-runner-cycle-limit-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    accounts = LocalAccountStore(root)
    state = BrowserNurtureStateStore(root)
    engine = _FakeNurtureEngine(frozenset())
    runner = BrowserNurtureRunner(LocalRuntime(root, accounts, engine), state)

    with pytest.raises(BrowserNurtureError) as error:
        asyncio.run(
            runner.run(
                account.alias,
                source=BrowserNurtureSource.BROWSER_UI,
                max_cycles=2,
            )
        )

    assert error.value.code == "RUN_POLICY_INVALID"
    assert engine.sessions == []


@pytest.mark.parametrize("idle_seconds", [0, 59])
def test_live_continuous_runner_enforces_configured_cooldown_with_cycle_cap(
    tmp_path: Path,
    idle_seconds: int,
) -> None:
    root = tmp_path / f"live-cooldown-{idle_seconds}-root"
    _secure_directory(root)
    account = _create_browser_source_account(root, "offline.operator")
    accounts = LocalAccountStore(root)
    state = BrowserNurtureStateStore(root)
    engine = _FakeNurtureEngine(frozenset())
    runner = BrowserNurtureRunner(LocalRuntime(root, accounts, engine), state)

    with pytest.raises(BrowserNurtureError) as error:
        asyncio.run(
            runner.run(
                account.alias,
                source=BrowserNurtureSource.BROWSER_UI,
                max_cycles=2,
                continuous=True,
                idle_seconds=idle_seconds,
            )
        )

    assert error.value.code == "RUN_POLICY_INVALID"
    assert engine.sessions == []
    assert state.load_checkpoint(account.id).completed_cycles == 0


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


@pytest.mark.parametrize(
    ("clicked_link", "expected_status", "expected_read_count"),
    [
        ("selected-post-link", "COMPLETE", 1),
        ("home-only-link", "STOPPED", 0),
    ],
)
def test_manual_assist_reads_only_a_trusted_search_result_and_preserves_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clicked_link: str,
    expected_status: str,
    expected_read_count: int,
) -> None:
    _require_local_chromium()
    root = tmp_path / f"manual-assist-{expected_status.casefold()}-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _manual_assist_pages(actor)
    opened_sessions: list[Any] = []
    requested_urls = _install_offline_live_browser_transport(
        monkeypatch,
        pages,
        opened_sessions=opened_sessions,
    )
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))

    checkpoint_path = root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json"
    state = BrowserNurtureStateStore(root)
    config = state.load_config(account.id)
    assert config is not None
    checkpoint = state.load_checkpoint(
        account.id,
        policy_fingerprint=browser_nurture_policy_fingerprint(config),
    )
    state.save_checkpoint(replace(checkpoint, completed_cycles=1, scanned=1))
    checkpoint_before = checkpoint_path.read_bytes()

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)
    prompt_count = 0
    decision_prompt_count = 0

    async def operator_input(*, timeout_seconds: float) -> str | None:
        nonlocal decision_prompt_count, prompt_count
        if prompt_count >= 2:
            assert timeout_seconds == 60
            assert not opened_sessions[-1]._page.is_closed()
            if clicked_link == "home-only-link" and decision_prompt_count == 0:
                decision_prompt_count += 1
                return "K\n"
            return "\n"
        assert timeout_seconds == 300
        page = opened_sessions[-1]._page
        if prompt_count == 0:
            await page.goto(f"{BROWSER_FEED_ORIGIN}/search?q=design")
        elif prompt_count == 1:
            await page.locator(f"#{clicked_link}").click()
        prompt_count += 1
        return "\n"

    monkeypatch.setattr(cli_module, "_read_terminal_line", operator_input)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "manual-assist",
            account.alias,
            "--confirm-live-read-only",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0, output.out + output.err
    assert f"status={expected_status}" in output.out
    assert "search_candidates=1" in output.out
    assert f"posts_read={expected_read_count}" in output.out
    assert "checkpoint=UNCHANGED effects=NONE" in output.out
    assert "ACTION CLOSE_BROWSER browser=OPEN" in output.out
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert requested_urls
    assert set(requested_urls).issubset(pages)
    assert all(
        secret not in output.out + output.err
        for secret in (
            actor,
            "synthetic.author",
            "Home feed must stay outside Search results.",
            _BODIES["001"],
            "/@synthetic.author/post/001",
            "design",
        )
    )
    if clicked_link == "home-only-link":
        assert "DETAIL_SELECTION_NOT_IN_RESULTS chromium=OPEN stage=DETAIL" in output.err
        assert "classification=UNKNOWN body=NOT_READ" in output.out
    else:
        assert "search_region=VERIFIED search_candidates=1" in output.out
        assert "classification=RELEVANT body=FULL_VISIBLE" in output.out
        assert "ERROR " not in output.err


def test_manual_assist_rejects_unscoped_search_container_without_reading_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_local_chromium()
    root = tmp_path / "manual-assist-unscoped-root"
    _secure_directory(root)
    actor = "offline.operator"
    account = _create_browser_source_account(root, actor)
    pages = _manual_assist_pages(actor, scoped_search=False)
    opened_sessions: list[Any] = []
    requested_urls = _install_offline_live_browser_transport(
        monkeypatch,
        pages,
        opened_sessions=opened_sessions,
    )
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", os.fspath(root))
    monkeypatch.setattr(ui_reader_module, "_READINESS_TIMEOUT_MS", 250)

    from threads_platform.standalone import __main__ as cli_module

    monkeypatch.setattr(cli_module, "_browser_nurture_interactive_console", lambda: True)
    prompt_count = 0

    async def operator_input(*, timeout_seconds: float) -> str | None:
        nonlocal prompt_count
        if prompt_count == 0:
            assert timeout_seconds == 300
            await opened_sessions[-1]._page.goto(f"{BROWSER_FEED_ORIGIN}/search?q=design")
            prompt_count += 1
            return "\n"
        assert timeout_seconds == 60
        assert not opened_sessions[-1]._page.is_closed()
        if prompt_count == 1:
            prompt_count += 1
            return "K\n"
        return None

    monkeypatch.setattr(cli_module, "_read_terminal_line", operator_input)
    exit_code = cli_module.main(
        (
            "browser-nurture",
            "manual-assist",
            account.alias,
            "--confirm-live-read-only",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert "SEARCH_RESULTS_REGION_UNPROVEN chromium=OPEN stage=SEARCH" in output.err
    assert "status=STOPPED" in output.out
    assert "search_region=NOT_VERIFIED search_candidates=0 posts_read=0" in output.out
    assert "checkpoint=UNCHANGED effects=NONE" in output.out
    assert "CLOSE_CONFIRMATION_TIMEOUT chromium=OPEN action=AUTO_CLOSE" in output.err
    assert not (root / "browser_nurture_v2" / "checkpoints" / f"{account.id}.json").exists()
    assert "Home feed must stay outside Search results." not in output.out + output.err
    assert "Home-only synthetic content." not in output.out + output.err
    assert all(url in pages for url in requested_urls)


def test_manual_assist_terminal_wait_is_bounded_without_blocking_async_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from threads_platform.standalone import __main__ as cli_module

    release = Event()
    reader_started = Event()

    class DelayedInput:
        def readline(self) -> str:
            reader_started.set()
            release.wait(timeout=2)
            return "continue\n"

    monkeypatch.setattr(cli_module.sys, "stdin", DelayedInput())

    async def responsive_read() -> str | None:
        read_task = asyncio.create_task(cli_module._read_terminal_line(timeout_seconds=1))
        await asyncio.sleep(0.02)
        assert reader_started.is_set()
        assert not read_task.done()
        release.set()
        return await read_task

    assert asyncio.run(responsive_read()) == "continue\n"

    release.clear()
    reader_started.clear()

    async def bounded_read() -> tuple[str | None, float]:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        value = await cli_module._read_terminal_line(timeout_seconds=0.02)
        return value, loop.time() - started_at

    value, elapsed = asyncio.run(bounded_read())
    release.set()
    assert reader_started.is_set()
    assert value is None
    assert elapsed < 0.2
