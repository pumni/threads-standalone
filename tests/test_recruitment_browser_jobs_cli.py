# pyright: reportPrivateUsage=false

from __future__ import annotations

import io
import json
import os
import socket
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast

import pytest

from threads_platform.standalone import recruitment_browser_jobs_cli as cli_module
from threads_platform.standalone.__main__ import main
from threads_platform.standalone.browser_job_watch import (
    MIN_CYCLE_INTERVAL_SECONDS,
    WatchReasonCode,
    WatchStatus,
)
from threads_platform.standalone.recruitment_job_store import (
    RecruitmentJobStore,
    RetentionPolicy,
    StoreSummary,
)

_BASE = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
_BODY = "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng."
_POLICY = RetentionPolicy(raw_text_days=30, derived_vacancy_days=90)


class _Clock:
    def __init__(self, now: datetime = _BASE) -> None:
        self.now = now
        self.tick = 10.0

    def now_utc(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


class _Stop:
    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped

    def is_set(self) -> bool:
        return self.stopped


class _Sleeper:
    def __init__(
        self,
        clock: _Clock,
        *,
        stop: _Stop | None = None,
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


def _url(index: int) -> str:
    return f"https://threads.example.invalid/@synthetic-shop/post-{index:03d}"


def _post(index: int, body: str | None = _BODY) -> dict[str, object]:
    return {
        "url": _url(index),
        "body": body,
        "body_visibility": "UNKNOWN" if body is None else "FULL_VISIBLE",
        "published_at_utc": "2026-10-12T09:00:00Z",
    }


def _fixture_document(
    cycles: int = 1,
    *,
    bodies: tuple[str | None, ...] | None = None,
    targets_per_cycle: int = 1,
    repeat_targets: bool = False,
) -> dict[str, object]:
    fixture_cycles: list[dict[str, object]] = []
    for cycle_index in range(cycles):
        posts = [
            _post(
                post_index + 1
                if repeat_targets
                else cycle_index * targets_per_cycle + post_index + 1,
                _BODY if bodies is None else bodies[cycle_index],
            )
            for post_index in range(targets_per_cycle)
        ]
        fixture_cycles.append(
            {
                "targets": [post["url"] for post in posts],
                "posts": posts,
            }
        )
    return {
        "schema_version": 1,
        "fixture_kind": "SYNTHETIC_ONLY",
        "cycles": fixture_cycles,
    }


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _private_paths(tmp_path: Path) -> tuple[Path, Path]:
    private_parent = tmp_path / "private"
    _secure_directory(private_parent)
    fixture_dir = private_parent / "fixtures"
    _secure_directory(fixture_dir)
    return private_parent, fixture_dir


def _caller_created_root(private_parent: Path) -> Path:
    root = private_parent / "root"
    _secure_directory(root)
    return root


def _write_fixture(directory: Path, document: object, *, name: str = "fixture.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _write_raw_fixture(directory: Path, raw: bytes, *, name: str = "fixture.json") -> Path:
    path = directory / name
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return path


def _args(
    root: Path,
    fixture: Path,
    *,
    account: str = "test-a",
    run_id: str = "run-synthetic-001",
    max_cycles: int = 1,
    max_posts: int = 20,
    interval: int = 15,
) -> Namespace:
    return Namespace(
        source="fixture",
        account=account,
        data_root=os.fspath(root),
        fixture_file=os.fspath(fixture),
        run_id=run_id,
        max_cycles=max_cycles,
        max_posts_per_cycle=max_posts,
        interval_minutes=interval,
    )


def _invoke(
    args: Namespace,
    *,
    clock: _Clock | None = None,
    sleeper: _Sleeper | None = None,
    stop: _Stop | None = None,
    input_stream: io.StringIO | None = None,
) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    code = cli_module.run_recruitment_browser_jobs_cli(
        args,
        stdout=output,
        stderr=errors,
        clock=clock,
        sleeper=sleeper,
        stop_signal=stop,
        input_stream=input_stream or io.StringIO(),
    )
    return code, output.getvalue(), errors.getvalue()


def _archive_summary(root: Path, alias: str) -> StoreSummary:
    account = cli_module.synthetic_account_uuid(alias)
    return RecruitmentJobStore(root, retention_policy=_POLICY).summary(account)


def test_one_cycle_runs_bj01_through_bj04a_and_redacts_output(tmp_path: Path) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    body = "SYNTHETIC-BODY-DO-NOT-PRINT-9001"
    fixture = _write_fixture(fixture_dir, _fixture_document(bodies=(body,)))
    args = _args(root, fixture, account="test-redacted")

    code, output, errors = _invoke(
        args,
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )

    assert code == 0, (output, errors)
    assert errors == ""
    assert "status=COMPLETE" in output
    assert "source=SYNTHETIC_FIXTURE" in output
    assert "completed_cycles=1" in output
    assert body not in output
    assert os.fspath(fixture) not in output
    assert "test-redacted" not in output
    assert str(cli_module.synthetic_account_uuid("test-redacted")) not in output
    summary = _archive_summary(root, "test-redacted")
    assert summary.observation_count == 1
    assert summary.post_count == 1


@pytest.mark.parametrize("cycle_count", (3, 5))
def test_three_to_five_fake_clock_cycles_archive_and_count_repeated_fixtures(
    tmp_path: Path,
    cycle_count: int,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    fixture = _write_fixture(
        fixture_dir,
        _fixture_document(cycles=cycle_count, repeat_targets=True),
    )
    args = _args(root, fixture, max_cycles=cycle_count)
    clock = _Clock()
    sleeper = _Sleeper(clock)

    code, output, errors = _invoke(
        args,
        clock=clock,
        sleeper=sleeper,
        stop=_Stop(),
    )

    assert code == 0
    assert errors == ""
    assert f"completed_cycles={cycle_count}" in output
    assert f"duplicate={cycle_count - 1}" in output
    assert sleeper.waits == [MIN_CYCLE_INTERVAL_SECONDS] * (cycle_count - 1)
    summary = _archive_summary(root, "test-a")
    assert summary.observation_count == cycle_count
    assert summary.post_count == 1


def test_max_cycle_and_post_limits_fail_before_creating_root(tmp_path: Path) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    root = private_parent / "root"

    code, output, errors = _invoke(
        _args(root, fixture, max_cycles=6),
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )

    assert code == 1
    assert output == ""
    assert errors == "ERROR WATCH_POLICY_INVALID\n"
    assert not root.exists()


def test_data_root_must_be_caller_created_and_cli_does_not_create_it(tmp_path: Path) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    root = private_parent / "root"

    code, output, errors = _invoke(
        _args(root, fixture),
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )

    assert code == 1
    assert output == ""
    assert errors == "ERROR DATA_ROOT_REQUIRED\n"
    assert not root.exists()

    code, _output, errors = _invoke(
        _args(root, fixture, max_posts=21),
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )
    assert code == 1
    assert errors == "ERROR WATCH_POLICY_INVALID\n"
    assert not root.exists()


def test_target_plan_change_conflicts_and_completed_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    first_fixture = _write_fixture(fixture_dir, _fixture_document(), name="first.json")
    args = _args(root, first_fixture)
    clock = _Clock()
    sleeper = _Sleeper(clock)

    first_code, first_output, _ = _invoke(args, clock=clock, sleeper=sleeper, stop=_Stop())
    assert first_code == 0
    assert "status=COMPLETE" in first_output

    replay_code, replay_output, replay_errors = _invoke(
        args, clock=clock, sleeper=sleeper, stop=_Stop()
    )
    assert replay_code == 0
    assert replay_errors == ""
    assert "status=COMPLETE" in replay_output
    assert "scanned=0" in replay_output
    assert _archive_summary(root, "test-a").observation_count == 1

    changed_document = _fixture_document()
    changed_cycle = cast(dict[str, object], cast(list[object], changed_document["cycles"])[0])
    changed_post = cast(dict[str, object], cast(list[object], changed_cycle["posts"])[0])
    changed_url = _url(2)
    changed_post["url"] = changed_url
    changed_cycle["targets"] = [changed_url]
    changed_fixture = _write_fixture(fixture_dir, changed_document, name="changed.json")
    changed_code, changed_output, changed_errors = _invoke(
        _args(root, changed_fixture),
        clock=clock,
        sleeper=sleeper,
        stop=_Stop(),
    )
    assert changed_code == 1
    assert changed_errors == ""
    assert "reason=CHECKPOINT_CONFLICT" in changed_output
    assert _archive_summary(root, "test-a").observation_count == 1


def test_stopped_multi_cycle_run_resumes_with_same_plan_and_run_id(tmp_path: Path) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    fixture = _write_fixture(fixture_dir, _fixture_document(cycles=2))
    args = _args(root, fixture, max_cycles=2)
    clock = _Clock()
    stop = _Stop()
    sleeper = _Sleeper(clock, stop=stop, stop_after_wait=1)

    stopped_code, stopped_output, _ = _invoke(
        args,
        clock=clock,
        sleeper=sleeper,
        stop=stop,
    )

    assert stopped_code == 130
    assert "status=STOPPED" in stopped_output
    assert "completed_cycles=1" in stopped_output
    assert _archive_summary(root, "test-a").observation_count == 1

    stop.stopped = False
    resumed_code, resumed_output, _ = _invoke(
        args,
        clock=clock,
        sleeper=sleeper,
        stop=stop,
    )
    assert resumed_code == 0
    assert "status=COMPLETE" in resumed_output
    assert "completed_cycles=2" in resumed_output
    assert _archive_summary(root, "test-a").observation_count == 2


def test_two_synthetic_aliases_are_account_scoped_in_one_root(tmp_path: Path) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    root = _caller_created_root(private_parent)

    for alias in ("test-a", "synthetic-b"):
        code, _output, errors = _invoke(
            _args(root, fixture, account=alias, run_id=f"run-{alias}"),
            clock=_Clock(),
            sleeper=_Sleeper(_Clock()),
            stop=_Stop(),
        )
        assert code == 0
        assert errors == ""

    assert _archive_summary(root, "test-a").observation_count == 1
    assert _archive_summary(root, "synthetic-b").observation_count == 1
    assert cli_module.synthetic_account_uuid("test-a") != cli_module.synthetic_account_uuid(
        "synthetic-b"
    )
    assert cli_module.synthetic_account_uuid("test-a").version == 4


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Liên hệ 09123456789", "FIXTURE_BODY_INVALID"),
        ("email jobs@example.test", "FIXTURE_BODY_INVALID"),
        ("See https://example.com/post", "FIXTURE_BODY_INVALID"),
        ("See ftp://example.invalid/file", "FIXTURE_BODY_INVALID"),
    ],
)
def test_fixture_rejects_contact_values_and_remote_urls(
    tmp_path: Path,
    body: str,
    expected: str,
) -> None:
    _private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document(bodies=(body,)))

    with pytest.raises(cli_module.BrowserJobsCliError) as error:
        cli_module._load_fixture(fixture)

    assert error.value.code == expected


def test_corrupt_utf8_duplicate_keys_and_inconsistent_target_plan_fail_closed(
    tmp_path: Path,
) -> None:
    _private_parent, fixture_dir = _private_paths(tmp_path)
    invalid_utf8 = _write_raw_fixture(fixture_dir, b"\xff", name="invalid-utf8.json")
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_INVALID"):
        cli_module._load_fixture(invalid_utf8)

    duplicate_key = _write_raw_fixture(
        fixture_dir,
        b'{"schema_version":1,"schema_version":1}',
        name="duplicate.json",
    )
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_INVALID"):
        cli_module._load_fixture(duplicate_key)

    escaped_surrogate = _write_raw_fixture(
        fixture_dir,
        (
            b'{"schema_version":1,"fixture_kind":"SYNTHETIC_ONLY","cycles":'
            b'[{"targets":["https://threads.example.invalid/@synthetic-shop/post-001"],'
            b'"posts":[{"url":"https://threads.example.invalid/@synthetic-shop/post-001",'
            b'"body":"\\ud800","body_visibility":"FULL_VISIBLE",'
            b'"published_at_utc":null}]}]}'
        ),
        name="surrogate.json",
    )
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_BODY_INVALID"):
        cli_module._load_fixture(escaped_surrogate)

    document = _fixture_document()
    cycle = cast(dict[str, object], cast(list[object], document["cycles"])[0])
    cycle["targets"] = [_url(2)]
    mismatched = _write_fixture(fixture_dir, document, name="mismatch.json")
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_TARGET_PLAN_INVALID"):
        cli_module._load_fixture(mismatched)


def test_invalid_later_cycle_is_rejected_before_root_or_engine_is_touched(
    tmp_path: Path,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    document = _fixture_document(cycles=2)
    cycles = cast(list[object], document["cycles"])
    second_cycle = cast(dict[str, object], cycles[1])
    wrong_url = _url(3)
    second_cycle["targets"] = [wrong_url]
    fixture = _write_fixture(fixture_dir, document)
    out, err = io.StringIO(), io.StringIO()

    code = cli_module.run_recruitment_browser_jobs_cli(
        _args(root, fixture, max_cycles=2),
        stdout=out,
        stderr=err,
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop_signal=_Stop(),
    )

    assert code == 1
    assert out.getvalue() == ""
    assert err.getvalue() == "ERROR FIXTURE_TARGET_PLAN_INVALID\n"
    assert list(root.iterdir()) == []


def test_fixture_cycle_and_post_count_caps_are_enforced(tmp_path: Path) -> None:
    _private_parent, fixture_dir = _private_paths(tmp_path)
    too_many_cycles = _write_fixture(
        fixture_dir,
        _fixture_document(cycles=6),
        name="too-many-cycles.json",
    )
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_CYCLE_COUNT_INVALID"):
        cli_module._load_fixture(too_many_cycles)

    too_many_posts = _write_fixture(
        fixture_dir,
        _fixture_document(targets_per_cycle=21),
        name="too-many-posts.json",
    )
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_TARGET_PLAN_INVALID"):
        cli_module._load_fixture(too_many_posts)


@pytest.mark.parametrize("separator", ["\\", "/"])
def test_oversize_and_traversal_fixture_paths_are_rejected(tmp_path: Path, separator: str) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    oversize = _write_raw_fixture(
        fixture_dir,
        b" " * (cli_module.MAX_FIXTURE_BYTES + 1),
        name="oversize.json",
    )
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_TOO_LARGE"):
        cli_module._load_fixture(oversize)

    traversal = fixture_dir / "missing.json"
    args = _args(private_parent / "root", traversal)
    args.fixture_file = f"{fixture_dir}{separator}..{separator}missing.json"
    code, output, errors = _invoke(
        args,
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )
    assert code == 1
    assert output == ""
    assert errors == "ERROR PATH_INVALID\n"
    assert not (private_parent / "root").exists()


def test_non_private_fixture_parent_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_dir = tmp_path / "public-fixtures"
    fixture_dir.mkdir(mode=0o755)
    path = fixture_dir / "fixture.json"
    path.write_text(json.dumps(_fixture_document()), encoding="utf-8")
    os.chmod(fixture_dir, 0o755)

    original_verify = cli_module._store_fs._verify_private_directory

    def deny_fixture_directory(path: Path) -> None:
        if path == fixture_dir:
            raise cli_module._store_fs.RecruitmentJobStoreError(
                cli_module._store_fs.StoreErrorCode.PERMISSIONS_UNVERIFIED
            )
        original_verify(path)

    monkeypatch.setattr(cli_module._store_fs, "_verify_private_directory", deny_fixture_directory)
    with pytest.raises(cli_module.BrowserJobsCliError, match="FIXTURE_UNSAFE"):
        cli_module._load_fixture(path)


def test_manual_mode_returns_unknown_without_reading_any_target_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_read(_path: Path) -> tuple[object, ...]:
        raise AssertionError("manual source must not read a target or fixture")

    monkeypatch.setattr(cli_module, "_load_fixture", fail_if_read)
    args = Namespace(
        source="manual",
        account=None,
        data_root=None,
        fixture_file=None,
        run_id=None,
    )
    output = io.StringIO()
    errors = io.StringIO()

    code = cli_module.run_recruitment_browser_jobs_cli(args, stdout=output, stderr=errors)

    assert code == 0
    assert output.getvalue() == "BROWSER_WATCH status=UNKNOWN source=MANUAL_URL reason=NO_TEXT\n"
    assert errors.getvalue() == ""
    assert list(tmp_path.iterdir()) == []


def test_browser_source_and_invalid_flags_are_rejected_with_fixed_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as browser_error:
        main(["recruitment", "browser-watch", "--source", "browser"])
    assert browser_error.value.code == 2
    assert capsys.readouterr().err == "ERROR CLI_ARGUMENT_INVALID\n"

    with pytest.raises(SystemExit) as option_error:
        main(
            [
                "recruitment",
                "browser-watch",
                "--source",
                "fixture",
                "--fixture-file",
                "PRIVATE_PATH_SHOULD_NOT_PRINT",
                "--unknown-option",
            ]
        )
    assert option_error.value.code == 2
    result = capsys.readouterr()
    assert result.err == "ERROR CLI_ARGUMENT_INVALID\n"
    assert "PRIVATE_PATH_SHOULD_NOT_PRINT" not in result.err


def test_manual_input_options_are_rejected_without_opening_them(tmp_path: Path) -> None:
    output = io.StringIO()
    errors = io.StringIO()
    args = Namespace(
        source="manual",
        account=None,
        data_root=None,
        fixture_file=os.fspath(tmp_path / "not-opened.json"),
        run_id=None,
    )

    code = cli_module.run_recruitment_browser_jobs_cli(args, stdout=output, stderr=errors)

    assert code == 1
    assert output.getvalue() == ""
    assert errors.getvalue() == "ERROR MANUAL_INPUT_UNSUPPORTED\n"

    args.fixture_file = None
    args.max_cycles = 2
    code = cli_module.run_recruitment_browser_jobs_cli(args, stdout=output, stderr=errors)
    assert code == 1
    assert errors.getvalue().endswith("ERROR MANUAL_INPUT_UNSUPPORTED\n")


def test_checkpoint_root_must_be_marked_private_and_empty_on_first_use(
    tmp_path: Path,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    root = private_parent / "root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "unexpected.json").write_text("{}", encoding="utf-8")
    os.chmod(root / "unexpected.json", 0o600)

    code, output, errors = _invoke(
        _args(root, fixture),
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )

    assert code == 1
    assert output == ""
    assert errors == "ERROR DATA_ROOT_NOT_SYNTHETIC\n"
    assert (root / "unexpected.json").read_text(encoding="utf-8") == "{}"


def test_checkpoint_root_symlink_fails_closed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows may not grant symlink creation to the test user")
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    target = _caller_created_root(private_parent)
    root_link = private_parent / "root-link"
    try:
        root_link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("host does not allow symlink creation")

    code, output, errors = _invoke(
        _args(root_link, fixture),
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )

    assert code == 1
    assert output == ""
    assert errors == "ERROR DATA_ROOT_UNSAFE\n"
    assert list(target.iterdir()) == []


def test_two_cycle_run_needs_a_foreground_terminal_before_touching_root(
    tmp_path: Path,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    fixture = _write_fixture(fixture_dir, _fixture_document(cycles=2))

    code, output, errors = _invoke(
        _args(root, fixture, max_cycles=2),
        input_stream=io.StringIO(""),
    )

    assert code == 1
    assert output == ""
    assert errors == "ERROR FOREGROUND_TTY_REQUIRED\n"
    assert list(root.iterdir()) == []


def test_console_stop_signal_recognizes_stop_and_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ready(
        streams: list[object], *_args: object
    ) -> tuple[list[object], list[object], list[object]]:
        return streams, [], []

    monkeypatch.setattr(cli_module.select, "select", ready)
    stop_command = cli_module._ConsoleStopSignal(io.StringIO("STOP\n"))
    stop_command._wait_posix(1)
    assert stop_command.is_set()

    end_of_input = cli_module._ConsoleStopSignal(io.StringIO(""))
    end_of_input._wait_posix(1)
    assert end_of_input.is_set()


def test_ctrl_c_while_waiting_returns_fixed_interrupt_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    fixture = _write_fixture(fixture_dir, _fixture_document(cycles=2))
    root = _caller_created_root(private_parent)

    class _InterruptSleeper:
        def sleep(self, _seconds: float) -> None:
            raise KeyboardInterrupt

    def force_tty(_stream: TextIO) -> bool:
        return True

    def interrupt_sleeper(_stop: cli_module._ConsoleStopSignal) -> _InterruptSleeper:
        return _InterruptSleeper()

    monkeypatch.setattr(cli_module, "_is_tty", force_tty)
    monkeypatch.setattr(
        cli_module,
        "_ConsoleSleeper",
        interrupt_sleeper,
    )
    code = main(
        [
            "recruitment",
            "browser-watch",
            "--source",
            "fixture",
            "--account",
            "test-a",
            "--data-root",
            os.fspath(root),
            "--fixture-file",
            os.fspath(fixture),
            "--run-id",
            "run-ctrl-c",
            "--max-cycles",
            "2",
        ]
    )

    assert code == 130
    assert capsys.readouterr().err == (
        "FOREGROUND_WAIT type STOP + Enter, EOF, or Ctrl+C to stop.\nERROR INTERRUPTED\n"
    )
    assert _archive_summary(root, "test-a").observation_count == 1


def test_runtime_adapter_has_no_browser_network_or_process_launch_imports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_parent, fixture_dir = _private_paths(tmp_path)
    root = _caller_created_root(private_parent)
    fixture = _write_fixture(fixture_dir, _fixture_document())
    args = _args(root, fixture)

    def deny_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is not permitted")

    monkeypatch.setattr(socket, "create_connection", deny_socket)
    cli_source = Path(cli_module.__file__).read_text(encoding="utf-8").casefold()
    assert "playwright" not in cli_source
    assert "subprocess" not in cli_source

    code, output, errors = _invoke(
        args,
        clock=_Clock(),
        sleeper=_Sleeper(_Clock()),
        stop=_Stop(),
    )
    assert code == 0
    assert "status=COMPLETE" in output
    assert errors == ""


def test_shared_cli_recruitment_manual_and_content_commands_remain_registered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["recruitment", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "browser-watch" in help_text
    assert "manual-list" in help_text
    assert "manual-review" in help_text
    assert "content-add" in help_text


def test_engine_result_formatter_only_emits_fixed_reasons_and_counts() -> None:
    from threads_platform.standalone.browser_job_watch import WatchRunResult

    result = WatchRunResult(
        status=WatchStatus.FAILED,
        reason_code=WatchReasonCode.CHECKPOINT_CONFLICT,
        scanned=2,
        errors=1,
        completed_cycles=1,
    )

    output = cli_module._format_result(result)

    assert "reason=CHECKPOINT_CONFLICT" in output
    assert "scanned=2" in output
    assert repr(cli_module.BrowserJobsCliError("CHECKPOINT_CONFLICT")) == (
        "BrowserJobsCliError('CHECKPOINT_CONFLICT')"
    )
    assert "next_due_utc=-" in output
