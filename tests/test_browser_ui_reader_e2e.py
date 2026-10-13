from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from threads_platform.standalone.recruitment_browser_jobs_cli import synthetic_account_uuid

_ORIGIN = "https://threads.example.invalid"
_QUERY = "synthetic recruitment roles"
_RECRUITER_BODY = "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng."


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _case_dirs(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    private = tmp_path / name
    _secure_directory(private)
    fixtures = private / "fixtures"
    _secure_directory(fixtures)
    root = private / "root"
    _secure_directory(root)
    return private, fixtures, root


def _target(username: str, post_id: int) -> str:
    return f"{_ORIGIN}/@{username}/post-{post_id:03d}"


def _shell(actor: str, main: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<style>body{margin:0}main{display:block}.post-card{height:240px;"
        "border-bottom:1px solid #ddd}header[role=banner]{height:36px}</style></head><body>"
        f'<header role="banner"><a href="/@{actor}">Account</a></header>{main}'
        "</body></html>"
    )


def _feed_card(target: str) -> str:
    username = target.split("/@", maxsplit=1)[1].split("/", maxsplit=1)[0]
    return (
        '<article class="post-card"><header>'
        f'<a href="/@{username}">@{username}</a>'
        f'<a href="{target}">time</a>'
        "</header><div dir=auto>Visible card</div></article>"
    )


def _detail(
    target: str, body: str | None, *, extra: str = "", author: str | None = None
) -> dict[str, str]:
    username = author or target.split("/@", maxsplit=1)[1].split("/", maxsplit=1)[0]
    body_html = "" if body is None else f'<div dir="auto">{body}</div>'
    main = (
        "<main><article><header>"
        f'<a href="/@{username}">@{username}</a>'
        f'<a href="{target}">time</a>'
        f"</header>{body_html}{extra}</article></main>"
    )
    return {"target": target, "html": _shell("b1-ui-e2e", main)}


def _manifest(*, actor: str = "b1-ui-e2e") -> dict[str, object]:
    recruiter = _target("synthetic-shop", 1)
    seeker = _target("synthetic-seeker", 2)
    quote = _target("synthetic-sharer", 3)
    partial = _target("synthetic-shop", 4)
    unknown = _target("synthetic-shop", 5)
    cards = (recruiter, seeker, quote, recruiter, partial, unknown)
    feed_main = (
        f'<main><form role="search"><input type="search" value="{_QUERY}"></form>'
        '<img src="https://assets.example.invalid/synthetic.png" alt="offline blocked asset">'
        + "".join(_feed_card(target) for target in cards)
        + '<aside><article><header><a href="/@synthetic-shop">sidebar author</a>'
        f'<a href="{recruiter}">sidebar post</a></header></article></aside></main>'
    )
    details = [
        _detail(recruiter, _RECRUITER_BODY),
        _detail(seeker, "Em đang tìm việc kế toán."),
        _detail(
            quote,
            "Mình chia sẻ bài này.",
            extra="<blockquote>Cần tuyển nhân viên lương 99 triệu/tháng.</blockquote>",
        ),
        _detail(
            partial,
            "A synthetic event description with details hidden below.<button>See more</button>",
        ),
        _detail(unknown, None),
    ]
    return {
        "schema_version": 1,
        "fixture_kind": "SYNTHETIC_BROWSER_UI",
        "query": _QUERY,
        "cycles": [{"feed_html": _shell(actor, feed_main), "detail_pages": details}],
    }


def _write_manifest(fixtures: Path, value: object, name: str = "b1-ui.json") -> Path:
    path = fixtures / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _threads_local_executable() -> str:
    executable = shutil.which("threads-local")
    if executable is None:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = Path(sys.executable).with_name(f"threads-local{suffix}")
        if candidate.is_file():
            executable = os.fspath(candidate)
    assert executable is not None, "run installed CLI acceptance under `uv run`"
    return executable


def _run_cli(
    fixtures: Path,
    root: Path,
    manifest: Path,
    *,
    account: str = "test-b1-ui-e2e",
    run_id: str = "b1-ui-e2e-001",
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("THREADS_LOCAL_DATA_ROOT", None)
    return subprocess.run(
        [
            _threads_local_executable(),
            "recruitment",
            "browser-watch",
            "--source",
            "ui-fixture",
            "--account",
            account,
            "--fixture-file",
            os.fspath(manifest),
            "--data-root",
            os.fspath(root),
            "--run-id",
            run_id,
            *extra_args,
        ],
        cwd=fixtures,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )


def _require_local_chromium() -> None:
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError:
            pytest.skip("Playwright Chromium cannot launch in this environment")
        browser.close()


def _json_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _objects(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [cast(dict[str, object], item) for item in cast(list[object], value)]


def test_installed_cli_runs_b1_reader_through_archive_and_checkpoint(tmp_path: Path) -> None:
    _require_local_chromium()
    _private, fixtures, root = _case_dirs(tmp_path, "b1-ui-positive")
    account = "test-b1-ui-e2e"
    manifest = _write_manifest(fixtures, _manifest())

    first = _run_cli(fixtures, root, manifest)

    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stderr == ""
    assert "BROWSER_UI_FIXTURE batches=3 candidates=9 targets=5 duplicates=4" in first.stdout
    assert "details=5 scrolls=2" in first.stdout
    assert re.search(r"blocked_requests=[1-9][0-9]*", first.stdout)
    assert "status=COMPLETE source=SYNTHETIC_FIXTURE" in first.stdout
    assert "scanned=9" in first.stdout
    assert "vacancy=1" in first.stdout
    assert "threads.example.invalid" not in first.stdout + first.stderr
    assert all(secret not in first.stdout + first.stderr for secret in (account, _RECRUITER_BODY))

    account_uuid = synthetic_account_uuid(account)
    archive_path = root / "job_vacancies_v1" / "accounts" / f"{account_uuid}.json"
    checkpoint_path = root / "browser_job_watch_v1" / "accounts" / f"{account_uuid}.json"
    archive_bytes = archive_path.read_bytes()
    checkpoint_bytes = checkpoint_path.read_bytes()
    archive = _json_document(archive_path)
    posts = _objects(archive["posts"])
    posts_by_target = {cast(str, post["canonical_post_url"]): post for post in posts}
    vacancy_revisions = [
        vacancy
        for post in posts
        for revision in _objects(post["revisions"])
        for vacancy in _objects(revision["vacancy_revisions"])
    ]
    assert len(vacancy_revisions) == 1
    fields = cast(dict[str, object], vacancy_revisions[0]["fields"])
    assert cast(dict[str, object], fields["role_title"])["state"] == "SOURCE_SPAN"
    assert cast(dict[str, object], fields["headcount"])["value"] == 2
    location = cast(dict[str, object], fields["location"])
    assert cast(dict[str, object], location["province_city"])["value"] == "TP.HCM"
    salary = cast(dict[str, object], fields["salary"])
    assert cast(dict[str, object], salary["amount_min"])["value"] == 8_000_000
    assert cast(dict[str, object], salary["amount_max"])["value"] == 12_000_000
    assert all(
        cast(dict[str, object], observation)["source"] == "SYNTHETIC_FIXTURE"
        for post in posts
        for observation in _objects(post["observations"])
    )
    partial_record = posts_by_target[_target("synthetic-shop", 4)]
    partial_observation = _objects(partial_record["observations"])[0]
    assert partial_observation["text_completeness"] == "PARTIAL"
    assert _objects(_objects(partial_record["revisions"])[0]["vacancy_revisions"]) == []
    unknown_record = posts_by_target[_target("synthetic-shop", 5)]
    assert _objects(unknown_record["observations"])[0]["text_completeness"] == "UNKNOWN"
    assert _json_document(checkpoint_path)["completed_cycles"] == 1

    replay = _run_cli(fixtures, root, manifest)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "status=COMPLETE source=SYNTHETIC_FIXTURE" in replay.stdout
    assert "scanned=0" in replay.stdout
    assert archive_path.read_bytes() == archive_bytes
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    changed = _write_manifest(fixtures, _manifest(), name="b1-ui-changed.json")
    changed_value = json.loads(changed.read_text(encoding="utf-8"))
    changed_value["cycles"][0]["detail_pages"][0]["html"] = _shell(
        "b1-ui-e2e",
        '<main><article><header><a href="/@synthetic-shop">@synthetic-shop</a>'
        f'<a href="{_target("synthetic-shop", 1)}">time</a></header>'
        '<div dir="auto">Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 9–12 triệu/tháng.</div>'
        "</article></main>",
    )
    changed.write_text(json.dumps(changed_value, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(changed, 0o600)
    conflict = _run_cli(fixtures, root, changed)
    assert conflict.returncode == 1
    assert "CHECKPOINT_CONFLICT" in conflict.stdout + conflict.stderr


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    (
        ("wrong-actor", "BROWSER_SESSION_MISMATCH"),
        ("login", "SESSION_EXPIRED"),
        ("challenge", "CHALLENGE_REQUIRED"),
        ("selector-drift", "BROWSER_SELECTOR_DRIFT"),
        ("empty-feed", "BROWSER_EMPTY_FEED"),
        ("wrong-root", "OBSERVATION_FAILED"),
        ("detail-author", "OBSERVATION_FAILED"),
        ("off-origin-redirect", "BROWSER_REDIRECT_DETECTED"),
        ("query-limit", "UI_FIXTURE_QUERY_INVALID"),
        ("card-limit", "BROWSER_CARD_LIMIT_EXCEEDED"),
        ("unsafe-body", "OBSERVATION_FAILED"),
    ),
)
def test_installed_cli_fails_closed_for_synthetic_reader_safety_cases(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    _require_local_chromium()
    _private, fixtures, root = _case_dirs(tmp_path, f"b1-ui-{scenario}")
    document = _manifest(actor="wrong-actor" if scenario == "wrong-actor" else "b1-ui-e2e")
    cycle = cast(dict[str, object], cast(list[object], document["cycles"])[0])
    if scenario == "login":
        cycle["feed_html"] = (
            '<!doctype html><html><body><form action="/login">'
            '<input type="password"></form></body></html>'
        )
    elif scenario == "challenge":
        cycle["feed_html"] = _shell(
            "b1-ui-e2e",
            '<main><iframe title="challenge"></iframe></main>',
        )
    elif scenario == "selector-drift":
        cycle["feed_html"] = _shell("b1-ui-e2e", "<section><p>Changed layout</p></section>")
    elif scenario == "empty-feed":
        cycle["feed_html"] = _shell(
            "b1-ui-e2e",
            f'<main><form role="search"><input type="search" value="{_QUERY}"></form></main>',
        )
    elif scenario == "wrong-root":
        target = _target("synthetic-shop", 1)
        details = cast(list[object], cycle["detail_pages"])
        first_detail = cast(dict[str, object], details[0])
        first_detail["html"] = _detail(_target("synthetic-shop", 9), _RECRUITER_BODY)["html"]
        first_detail["target"] = target
    elif scenario == "detail-author":
        details = cast(list[object], cycle["detail_pages"])
        first_detail = cast(dict[str, object], details[0])
        first_detail["html"] = _detail(
            _target("synthetic-shop", 1),
            _RECRUITER_BODY,
            author="synthetic-wrong",
        )["html"]
    elif scenario == "off-origin-redirect":
        cycle["feed_html"] = (
            '<!doctype html><html><head><meta http-equiv="refresh" '
            'content="0;url=https://redirect.example.invalid/elsewhere"></head><body></body></html>'
        )
    elif scenario == "query-limit":
        document["query"] = "q" * 121
    elif scenario == "card-limit":
        target = _target("synthetic-shop", 1)
        cards = _feed_card(target) * 21
        cycle["feed_html"] = _shell(
            "b1-ui-e2e",
            f'<main><form role="search"><input type="search" value="{_QUERY}"></form>'
            f"<style>.post-card{{height:20px}}</style>{cards}</main>",
        )
    elif scenario == "unsafe-body":
        details = cast(list[object], cycle["detail_pages"])
        first_detail = cast(dict[str, object], details[0])
        first_detail["html"] = _detail(
            _target("synthetic-shop", 1),
            "Cần tuyển 02 sales; [synthetic contact] fake@example.invalid",
        )["html"]
    manifest = _write_manifest(fixtures, document)

    result = _run_cli(fixtures, root, manifest, run_id=f"b1-{scenario}")

    expected_exit_code = 130 if scenario == "detail-author" else 1
    assert result.returncode == expected_exit_code
    assert expected_error in result.stdout + result.stderr, result.stdout + result.stderr
    if scenario == "wrong-root":
        assert "status=FAILED" in result.stdout
        assert "vacancy=0" in result.stdout
    if scenario == "detail-author":
        assert "status=STOPPED" in result.stdout
        assert "vacancy=0" in result.stdout
    if scenario == "unsafe-body":
        assert "status=FAILED" in result.stdout
        assert "vacancy=0" in result.stdout
    assert "threads.example.invalid" not in result.stdout + result.stderr


def test_browser_source_is_disabled_before_operator_path_access(tmp_path: Path) -> None:
    _private, _fixtures, _root = _case_dirs(tmp_path, "b1-browser-disabled")
    operator_root = tmp_path / "not-created-operator-root"
    environment = os.environ.copy()
    environment.pop("THREADS_LOCAL_DATA_ROOT", None)

    result = subprocess.run(
        [
            _threads_local_executable(),
            "recruitment",
            "browser-watch",
            "--source",
            "browser",
            "--data-root",
            os.fspath(operator_root),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert (
        "status=DISABLED_PERMISSION_PENDING source=BROWSER_UI reason=SOURCE_DISABLED"
        in result.stdout
    )
    assert result.stderr == ""
    assert not operator_root.exists()
