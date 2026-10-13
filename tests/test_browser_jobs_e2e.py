"""Synthetic-only cross-layer acceptance for the Browser Jobs CLI."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from threads_platform.standalone import recruitment_browser_jobs_cli as cli_module
from threads_platform.standalone.__main__ import main
from threads_platform.standalone.recruitment_job_store import (
    ExportField,
    RecruitmentJobStore,
    RetentionPolicy,
)

_PUBLISHED_AT = "2026-10-12T09:00:00Z"
_BASE = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
_POLICY = RetentionPolicy(raw_text_days=30, derived_vacancy_days=90)


class _FakeClock:
    def __init__(self) -> None:
        self.now = _BASE
        self.tick = 0.0

    def monotonic(self) -> float:
        return self.tick

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


class _FakeStop:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped


class _FakeSleeper:
    def __init__(
        self,
        clock: _FakeClock,
        *,
        stop: _FakeStop | None = None,
        stop_after_wait: int | None = None,
    ) -> None:
        self.clock = clock
        self.stop = stop
        self.stop_after_wait = stop_after_wait
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)
        if self.stop is not None and len(self.waits) == self.stop_after_wait:
            self.stop.stopped = True


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _case_dirs(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    private_parent = tmp_path / name
    _secure_directory(private_parent)
    fixture_dir = private_parent / "fixtures"
    _secure_directory(fixture_dir)
    data_root = private_parent / "data-root"
    _secure_directory(data_root)
    return private_parent, fixture_dir, data_root


def _url(index: int) -> str:
    return f"https://threads.example.invalid/@synthetic-shop/post-{index:03d}"


def _post(
    index: int,
    body: str | None,
    *,
    visibility: str | None = None,
) -> dict[str, object]:
    return {
        "url": _url(index),
        "body": body,
        "body_visibility": visibility or ("UNKNOWN" if body is None else "FULL_VISIBLE"),
        "published_at_utc": _PUBLISHED_AT,
    }


def _fixture(cycles: tuple[tuple[dict[str, object], ...], ...]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "fixture_kind": "SYNTHETIC_ONLY",
        "cycles": [
            {"targets": [post["url"] for post in posts], "posts": list(posts)} for posts in cycles
        ],
    }


def _write_fixture(fixture_dir: Path, document: object, name: str = "fixture.json") -> Path:
    path = fixture_dir / name
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _args(
    root: Path,
    fixture: Path,
    *,
    account: str = "test-bj07-a",
    run_id: str = "bj07-synthetic-run",
    max_cycles: int = 1,
    max_posts: int = 20,
) -> argparse.Namespace:
    return argparse.Namespace(
        source="fixture",
        account=account,
        data_root=os.fspath(root),
        fixture_file=os.fspath(fixture),
        run_id=run_id,
        max_cycles=max_cycles,
        max_posts_per_cycle=max_posts,
        interval_minutes=15,
    )


def _invoke(
    args: argparse.Namespace,
    *,
    clock: _FakeClock | None = None,
    sleeper: _FakeSleeper | None = None,
    stop: _FakeStop | None = None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli_module.run_recruitment_browser_jobs_cli(
        args,
        stdout=stdout,
        stderr=stderr,
        clock=clock,
        sleeper=sleeper,
        stop_signal=stop,
        input_stream=io.StringIO(),
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _archive_path(root: Path, alias: str) -> Path:
    account_uuid = cli_module.synthetic_account_uuid(alias)
    return root / "job_vacancies_v1" / "accounts" / f"{account_uuid}.json"


def _checkpoint_path(root: Path, alias: str) -> Path:
    account_uuid = cli_module.synthetic_account_uuid(alias)
    return root / "browser_job_watch_v1" / "accounts" / f"{account_uuid}.json"


def _document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _objects(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_object(item) for item in cast(list[object], value)]


def _post_record(archive: dict[str, object], target: str) -> dict[str, object]:
    records = _objects(archive["posts"])
    return next(item for item in records if item["canonical_post_url"] == target)


def _entrypoint_args(account: str, root: Path, fixture: Path, run_id: str) -> list[str]:
    return [
        "recruitment",
        "browser-watch",
        "--source",
        "fixture",
        "--account",
        account,
        "--data-root",
        os.fspath(root),
        "--fixture-file",
        os.fspath(fixture),
        "--run-id",
        run_id,
    ]


def _dom_card(index: int, handle: str) -> str:
    url = _url(index).replace("synthetic-shop", handle)
    return (
        "<article data-post-card>"
        f'<a data-post-permalink href="{url}">open</a>'
        f"<header data-post-header><span data-post-author>@{handle}</span></header>"
        "</article>"
    )


def _dom_detail(
    index: int, handle: str, body_html: str, *, kind: str = "ORIGINAL"
) -> dict[str, str]:
    url = _url(index).replace("synthetic-shop", handle)
    html = (
        "<main data-post-detail>"
        f'<article data-root-post data-post-kind="{kind}">'
        f'<a data-post-permalink href="{url}">root</a>'
        f"<header data-post-header><span data-post-author>@{handle}</span></header>"
        f"{body_html}"
        "</article></main>"
    )
    return {"target": url, "html": html}


def _dom_manifest(recruiter_body: str) -> dict[str, object]:
    query = "synthetic recruitment roles"
    feed_html = (
        f'<main data-search-query="{query}">'
        '<img src="https://assets.example.invalid/blocked.png" alt="synthetic blocked image">'
        '<section data-feed-tranche="0" style="height: 900px">'
        f"{_dom_card(1, 'synthetic-shop')}{_dom_card(2, 'synthetic-seeker')}"
        "</section>"
        '<section data-feed-tranche="1" style="height: 900px">'
        f"{_dom_card(1, 'synthetic-shop')}{_dom_card(3, 'synthetic-sharer')}"
        "</section>"
        "<aside><p>Cần tuyển hidden sidebar content</p></aside>"
        "</main>"
    )
    details = [
        _dom_detail(
            1,
            "synthetic-shop",
            f'<div data-own-post-body data-body-visibility="FULL_VISIBLE">{recruiter_body}</div>',
        ),
        _dom_detail(
            2,
            "synthetic-seeker",
            '<div data-own-post-body data-body-visibility="FULL_VISIBLE">'
            "<span hidden>Cần tuyển 99 nhân viên lương 99 triệu.</span>"
            "Em đang tìm việc kế toán.</div>",
        ),
        _dom_detail(
            3,
            "synthetic-sharer",
            '<div data-own-post-body data-body-visibility="FULL_VISIBLE" data-collapsed="true">'
            "Mình chia sẻ bài này.</div>"
            "<aside data-quoted-post><blockquote>"
            "Cần tuyển nhân viên bán hàng tại Hà Nội; lương 99 triệu/tháng."
            "</blockquote></aside>",
            kind="QUOTE",
        ),
    ]
    return {
        "schema_version": 1,
        "fixture_kind": "SYNTHETIC_DOM_ONLY",
        "query": query,
        "cycles": [{"feed_html": feed_html, "detail_pages": details}],
    }


def _write_dom_manifest(
    fixture_dir: Path, document: object, name: str = "dom-fixture.json"
) -> Path:
    path = fixture_dir / name
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
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


def _run_installed_dom_cli(
    account: str,
    root: Path,
    fixture: Path,
    run_id: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("THREADS_LOCAL_DATA_ROOT", None)
    return subprocess.run(
        [
            _threads_local_executable(),
            "recruitment",
            "browser-watch",
            "--source",
            "dom-fixture",
            "--account",
            account,
            "--fixture-file",
            os.fspath(fixture),
            "--data-root",
            os.fspath(root),
            "--run-id",
            run_id,
        ],
        cwd=fixture.parent,
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
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-background-networking", "--disable-sync", "--no-first-run"],
            )
        except PlaywrightError:
            pytest.skip("Playwright Chromium cannot launch in this environment")
        browser.close()


def test_installed_cli_renders_dom_and_archives_only_recruiter_vacancy(
    tmp_path: Path,
) -> None:
    _require_local_chromium()
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, "dom-cli-e2e")
    alias = "test-dom-e2e"
    run_id = "dom-e2e-001"
    recruiter_body = "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng."
    fixture = _write_dom_manifest(fixture_dir, _dom_manifest(recruiter_body))

    first = _run_installed_dom_cli(alias, root, fixture, run_id)

    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stderr == ""
    assert "DOM_FIXTURE batches=2 candidates=4 targets=3 duplicates=1 details=3" in first.stdout
    assert "blocked_requests=1" in first.stdout
    assert "status=COMPLETE source=SYNTHETIC_FIXTURE" in first.stdout
    assert "scanned=4" in first.stdout
    assert "vacancy=1" in first.stdout
    for private_value in (alias, os.fspath(root), os.fspath(fixture), recruiter_body):
        assert private_value not in first.stdout + first.stderr
    assert "threads.example.invalid" not in first.stdout + first.stderr

    archive_path = _archive_path(root, alias)
    checkpoint_path = _checkpoint_path(root, alias)
    archive_bytes = archive_path.read_bytes()
    checkpoint_bytes = checkpoint_path.read_bytes()
    archive = _document(archive_path)
    posts = _objects(archive["posts"])
    assert len(posts) == 3
    assert all(_objects(post["observations"])[0]["source"] == "SYNTHETIC_FIXTURE" for post in posts)
    recruiter_record = _post_record(archive, _url(1))
    seeker_record = _post_record(
        archive,
        _url(2).replace("synthetic-shop", "synthetic-seeker"),
    )
    quote_record = _post_record(
        archive,
        _url(3).replace("synthetic-shop", "synthetic-sharer"),
    )
    vacancy_revisions = [
        vacancy
        for post in posts
        for revision in _objects(post["revisions"])
        for vacancy in _objects(revision["vacancy_revisions"])
    ]
    assert len(vacancy_revisions) == 1
    assert _objects(_objects(seeker_record["revisions"])[0]["vacancy_revisions"]) == []
    assert _objects(_objects(quote_record["revisions"])[0]["vacancy_revisions"]) == []
    fields = _object(vacancy_revisions[0]["fields"])
    assert _object(fields["role_title"])["state"] == "SOURCE_SPAN"
    assert _object(fields["headcount"])["value"] == 2
    location = _object(fields["location"])
    assert _object(location["province_city"])["value"] == "TP.HCM"
    salary = _object(fields["salary"])
    assert _object(salary["amount_min"])["value"] == 8_000_000
    assert _object(salary["amount_max"])["value"] == 12_000_000
    assert _objects(_objects(recruiter_record["revisions"])[0]["vacancy_revisions"])
    assert "https://threads.example.invalid" not in json.dumps(
        _document(checkpoint_path), ensure_ascii=False
    )
    assert _document(checkpoint_path)["completed_cycles"] == 1

    replay = _run_installed_dom_cli(alias, root, fixture, run_id)

    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "status=COMPLETE source=SYNTHETIC_FIXTURE" in replay.stdout
    assert "scanned=0" in replay.stdout
    assert "vacancy=0" in replay.stdout
    assert archive_path.read_bytes() == archive_bytes
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    changed = _write_dom_manifest(
        fixture_dir,
        _dom_manifest("Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 9–12 triệu/tháng."),
        name="dom-fixture-changed.json",
    )
    conflict = _run_installed_dom_cli(alias, root, changed, run_id)

    assert conflict.returncode == 1
    assert "status=FAILED source=SYNTHETIC_FIXTURE reason=CHECKPOINT_CONFLICT" in conflict.stdout
    assert archive_path.read_bytes() == archive_bytes
    assert checkpoint_path.read_bytes() == checkpoint_bytes


def test_one_cycle_cli_archives_parser_evidence_and_redacts_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, "account-a")
    alias = "test-bj07-a"
    recruiter = (
        "Cần tuyển sales và kế toán; Lương sales 8tr/tháng; sales tại Hà Nội; kế toán tại TP.HCM"
    )
    seeker = "Em đang tìm việc kế toán"
    unrelated = "Bài viết tổng hợp dự báo thời tiết hôm nay"
    partial = "Cần tuyển công nhân ca đêm, lương 25k/giờ"
    bodies = (recruiter, seeker, unrelated, partial, None)
    posts = tuple(
        _post(index, body, visibility="PARTIAL" if index == 4 else None)
        for index, body in enumerate(bodies, start=1)
    )
    fixture = _write_fixture(fixture_dir, _fixture((posts,)))

    result = main(_entrypoint_args(alias, root, fixture, "bj07-one-cycle"))
    captured = capsys.readouterr()

    assert result == 0
    assert captured.err == ""
    assert "status=COMPLETE" in captured.out
    assert "source=SYNTHETIC_FIXTURE" in captured.out
    assert "scanned=5" in captured.out
    for private_value in (alias, os.fspath(root), os.fspath(fixture), *bodies[:4]):
        assert private_value not in captured.out + captured.err
    assert "threads.example.invalid" not in captured.out + captured.err

    archive_path = _archive_path(root, alias)
    checkpoint_path = _checkpoint_path(root, alias)
    assert archive_path.is_file()
    assert checkpoint_path.is_file()
    account_uuid = cli_module.synthetic_account_uuid(alias)
    assert (root / "job_vacancies_v1" / "locks" / f"{account_uuid}.lock").is_file()
    assert (root / "browser_job_watch_v1" / "locks" / f"{account_uuid}.lock").is_file()

    archive = _document(archive_path)
    assert archive["schema_version"] == 1
    assert archive["account_uuid"] == str(account_uuid)
    assert len(_objects(archive["posts"])) == 5
    assert "https://threads.example.invalid" not in json.dumps(
        _document(checkpoint_path), ensure_ascii=False
    )

    recruiter_record = _post_record(archive, _url(1))
    recruiter_event = _objects(recruiter_record["observations"])[0]
    assert recruiter_event["source"] == "SYNTHETIC_FIXTURE"
    assert recruiter_event["text_provenance"] == "SYNTHETIC_FIXTURE_TEXT"
    assert recruiter_event["completeness_basis"] == "FIXTURE_DECLARED"
    recruiter_revision = _objects(recruiter_record["revisions"])[0]
    vacancy_revisions = _objects(recruiter_revision["vacancy_revisions"])
    assert {item["role_anchor"] for item in vacancy_revisions} == {
        "role-v1:sales",
        "role-v1:accounting",
    }
    by_role = {str(item["role_anchor"]): item for item in vacancy_revisions}
    sales_fields = _object(by_role["role-v1:sales"]["fields"])
    accounting_fields = _object(by_role["role-v1:accounting"]["fields"])
    sales_salary = _object(sales_fields["salary"])
    accounting_salary = _object(accounting_fields["salary"])
    assert _object(sales_salary["amount_min"])["value"] == 8_000_000
    assert _object(accounting_salary["amount_min"])["state"] == "ABSENT"
    assert _object(_object(accounting_fields["location"])["province_city"])["value"] == "TP.HCM"

    seeker_record = _post_record(archive, _url(2))
    assert _objects(_objects(seeker_record["revisions"])[0]["vacancy_revisions"]) == []
    partial_record = _post_record(archive, _url(4))
    assert _objects(partial_record["observations"])[0]["text_completeness"] == "PARTIAL"
    unknown_record = _post_record(archive, _url(5))
    unknown_event = _objects(unknown_record["observations"])[0]
    assert unknown_event["status"] == "UNKNOWN"
    assert unknown_event["text_completeness"] == "UNKNOWN"
    assert unknown_event["text_provenance"] == "NONE"


@pytest.mark.parametrize("cycle_count", (3, 5))
def test_fake_clock_cycles_dedupe_revise_replay_and_reject_changed_plan(
    tmp_path: Path,
    cycle_count: int,
) -> None:
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, f"cycles-{cycle_count}")
    alias = f"test-bj07-{cycle_count}"
    original = "Cần tuyển sales; lương 8tr/tháng"
    changed = "Cần tuyển sales; lương 9tr/tháng"
    bodies = tuple(original if index < 2 else changed for index in range(cycle_count))
    cycle_posts = tuple((_post(10, body),) for body in bodies)
    fixture = _write_fixture(fixture_dir, _fixture(cycle_posts))
    args = _args(root, fixture, account=alias, max_cycles=cycle_count)
    clock = _FakeClock()
    sleeper = _FakeSleeper(clock)

    code, output, errors = _invoke(args, clock=clock, sleeper=sleeper, stop=_FakeStop())

    assert code == 0
    assert errors == ""
    assert f"completed_cycles={cycle_count}" in output
    assert sleeper.waits == [900.0] * (cycle_count - 1)
    assert clock.now - _BASE >= timedelta(minutes=15 * (cycle_count - 1))
    summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(
        cli_module.synthetic_account_uuid(alias)
    )
    assert summary.post_count == 1
    assert summary.observation_count == cycle_count
    assert summary.revision_count == 2

    archive_before = _archive_path(root, alias).read_bytes()
    checkpoint_before = _checkpoint_path(root, alias).read_bytes()
    replay_clock = _FakeClock()
    replay_code, replay_output, replay_errors = _invoke(
        args,
        clock=replay_clock,
        sleeper=_FakeSleeper(replay_clock),
        stop=_FakeStop(),
    )
    assert replay_code == 0
    assert replay_errors == ""
    assert "status=COMPLETE" in replay_output
    assert "scanned=0" in replay_output
    assert _archive_path(root, alias).read_bytes() == archive_before
    assert _checkpoint_path(root, alias).read_bytes() == checkpoint_before

    changed_document = _fixture(((_post(11, original),),) * cycle_count)
    changed_fixture = _write_fixture(fixture_dir, changed_document, name="changed-plan.json")
    conflict_code, conflict_output, conflict_errors = _invoke(
        _args(root, changed_fixture, account=alias, max_cycles=cycle_count),
        clock=_FakeClock(),
        sleeper=_FakeSleeper(_FakeClock()),
        stop=_FakeStop(),
    )
    assert conflict_code == 1
    assert conflict_errors == ""
    assert "reason=CHECKPOINT_CONFLICT" in conflict_output
    assert _archive_path(root, alias).read_bytes() == archive_before
    assert _checkpoint_path(root, alias).read_bytes() == checkpoint_before


def test_stop_then_resume_keeps_the_same_private_fixture_plan(tmp_path: Path) -> None:
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, "stop-resume")
    alias = "test-bj07-resume"
    posts_by_cycle = ((_post(20, "Cần tuyển sales"),), (_post(21, "Cần tuyển kế toán"),))
    fixture = _write_fixture(fixture_dir, _fixture(posts_by_cycle))
    args = _args(root, fixture, account=alias, run_id="bj07-resume", max_cycles=2)
    clock = _FakeClock()
    stop = _FakeStop()
    first_sleeper = _FakeSleeper(clock, stop=stop, stop_after_wait=1)

    stopped_code, stopped_output, stopped_errors = _invoke(
        args, clock=clock, sleeper=first_sleeper, stop=stop
    )

    assert stopped_code == 130
    assert stopped_errors == ""
    assert "status=STOPPED" in stopped_output
    assert "completed_cycles=1" in stopped_output
    assert len(_objects(_document(_archive_path(root, alias))["posts"])) == 1

    stop.stopped = False
    resumed_code, resumed_output, resumed_errors = _invoke(
        args,
        clock=clock,
        sleeper=_FakeSleeper(clock),
        stop=stop,
    )

    assert resumed_code == 0
    assert resumed_errors == ""
    assert "status=COMPLETE" in resumed_output
    assert "completed_cycles=2" in resumed_output
    assert len(_objects(_document(_archive_path(root, alias))["posts"])) == 2
    assert _document(_checkpoint_path(root, alias))["completed_cycles"] == 2


def test_account_roots_are_isolated_and_manual_or_live_sources_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounts = (("test-bj07-a", 30), ("synthetic-bj07-b", 31))
    roots: list[Path] = []
    account_ids: list[UUID] = []
    for alias, index in accounts:
        _private_parent, fixture_dir, root = _case_dirs(tmp_path, alias)
        fixture = _write_fixture(
            fixture_dir,
            _fixture(((_post(index, "Cần tuyển nhân viên bán hàng"),),)),
        )
        result = main(_entrypoint_args(alias, root, fixture, f"run-{index}"))
        captured = capsys.readouterr()
        assert result == 0
        assert captured.err == ""
        assert alias not in captured.out
        roots.append(root)
        account_ids.append(cli_module.synthetic_account_uuid(alias))

    assert account_ids[0] != account_ids[1]
    assert _archive_path(roots[0], accounts[0][0]).is_file()
    assert _archive_path(roots[1], accounts[1][0]).is_file()
    assert _checkpoint_path(roots[0], accounts[0][0]).is_file()
    assert _checkpoint_path(roots[1], accounts[1][0]).is_file()
    assert (
        _archive_path(roots[0], accounts[0][0]).parent
        != _archive_path(roots[1], accounts[1][0]).parent
    )

    manual_code = main(["recruitment", "browser-watch", "--source", "manual"])
    manual_output = capsys.readouterr()
    assert manual_code == 0
    assert manual_output.out == ("BROWSER_WATCH status=UNKNOWN source=MANUAL_URL reason=NO_TEXT\n")
    assert manual_output.err == ""

    with pytest.raises(SystemExit) as browser_error:
        main(["recruitment", "browser-watch", "--source", "browser"])
    browser_output = capsys.readouterr()
    assert browser_error.value.code == 2
    assert browser_output.out == ""
    assert browser_output.err == "ERROR CLI_ARGUMENT_INVALID\n"


@pytest.mark.parametrize(
    ("cycle_count", "post_count", "max_cycles", "max_posts", "reason"),
    (
        (1, 1, 6, 20, "WATCH_POLICY_INVALID"),
        (1, 21, 1, 21, "WATCH_POLICY_INVALID"),
        (6, 1, 5, 20, "FIXTURE_CYCLE_COUNT_INVALID"),
    ),
)
def test_cycle_and_post_caps_fail_before_archive_or_checkpoint_creation(
    tmp_path: Path,
    cycle_count: int,
    post_count: int,
    max_cycles: int,
    max_posts: int,
    reason: str,
) -> None:
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, f"caps-{cycle_count}-{post_count}")
    cycles = tuple(
        tuple(_post(100 + index, "Cần tuyển nhân viên") for index in range(post_count))
        for _ in range(cycle_count)
    )
    fixture = _write_fixture(fixture_dir, _fixture(cycles))
    code, output, errors = _invoke(
        _args(root, fixture, max_cycles=max_cycles, max_posts=max_posts),
        clock=_FakeClock(),
        sleeper=_FakeSleeper(_FakeClock()),
        stop=_FakeStop(),
    )

    assert code == 1
    assert reason in output + errors
    assert not (root / "job_vacancies_v1").exists()
    assert not (root / "browser_job_watch_v1").exists()


def test_explicit_export_hides_synthetic_contact_after_raw_expiry_and_escapes_formulas(
    tmp_path: Path,
) -> None:
    _private_parent, fixture_dir, root = _case_dirs(tmp_path, "export-privacy")
    alias = "test-bj07-export"
    contact = "[SYNTHETIC_PHONE_001]"
    contact_body = f"Cần tuyển sales yêu cầu: liên hệ {contact}"
    formula_body = "Cần tuyển kế toán; yêu cầu: =1+1"
    fixture = _write_fixture(
        fixture_dir,
        _fixture(((_post(40, contact_body), _post(41, formula_body)),)),
    )
    result = main(_entrypoint_args(alias, root, fixture, "bj07-export"))
    assert result == 0

    account_uuid = cli_module.synthetic_account_uuid(alias)
    initial_archive = _document(_archive_path(root, alias))
    contact_identity = _post_record(initial_archive, _url(40))["identity_sha256"]
    store = RecruitmentJobStore(root, retention_policy=_POLICY)
    expiry = datetime.now(UTC) + timedelta(days=31)
    selected_fields = (ExportField.ROLE_TITLE, ExportField.REQUIREMENTS)
    json_export = store.export_json(
        account_uuid,
        fields=selected_fields,
        confirmed=True,
        now_utc=expiry,
    )
    csv_export = store.export_csv(
        account_uuid,
        fields=selected_fields,
        confirmed=True,
        now_utc=expiry,
    )

    assert contact not in json_export + csv_export
    assert "https://threads.example.invalid" not in json_export + csv_export
    exported = json.loads(json_export)
    assert isinstance(exported, dict)
    assert exported["fields"] == [field.value for field in selected_fields]
    csv_rows = list(csv.reader(io.StringIO(csv_export)))
    csv_cells = [cell for row in csv_rows for cell in row]
    assert "'=1+1" in csv_cells

    archive = _document(_archive_path(root, alias))
    assert contact not in json.dumps(archive, ensure_ascii=False)
    contact_record = next(
        item for item in _objects(archive["posts"]) if item["identity_sha256"] == contact_identity
    )
    contact_revision = _objects(contact_record["revisions"])[0]
    assert contact_revision["observed_text"] is None
    vacancy = _objects(contact_revision["vacancy_revisions"])[0]
    requirement = _object(_object(vacancy["fields"])["requirements"])
    assert requirement["state"] == "AMBIGUOUS"
    assert requirement["value"] is None
    assert requirement["raw_value"] is None
    assert requirement["spans"] == []


def test_nested_browser_watch_help_exposes_only_synthetic_and_manual_sources(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["recruitment", "browser-watch", "--help"])

    captured = capsys.readouterr()
    assert help_exit.value.code == 0
    assert "--source {fixture,dom-fixture,manual}" in captured.out
    assert "--fixture-file" in captured.out
    assert "browser}" not in captured.out
