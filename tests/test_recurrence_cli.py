from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import threads_platform.standalone.__main__ as cli_module
from threads_platform.standalone.__main__ import main


def _set_data_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))


def _write_workflow(path: Path, steps: list[object], *, account: str = "alice") -> Path:
    path.write_text(
        json.dumps({"version": 1, "account": account, "steps": steps}),
        encoding="utf-8",
    )
    return path


def _assert_installed_wheel_when_requested() -> None:
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(cli_module.__file__).resolve()).casefold()


def test_cli_recurrence_create_list_show_keep_workflow_metadata_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_installed_wheel_when_requested()
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()
    query_sentinel = "private-recurrence-query-sentinel"
    source = tmp_path / "private-recurrence-source.json"
    _write_workflow(
        source,
        [
            {
                "action": "search",
                "query": query_sentinel,
                "mode": "KEYWORD",
                "type": "TOP",
                "limit": 3,
            }
        ],
    )

    assert main(["recurrence", "create", str(source), "--every-seconds", "300"]) == 0
    created = capsys.readouterr()
    assert created.err == ""
    match = re.fullmatch(
        r"recurrence created id=([0-9a-f-]{36}) account=alice every_seconds=300 "
        r"next_due_at=[0-9TZ:.-]+\n",
        created.out,
    )
    assert match is not None
    recurrence_id = match.group(1)

    assert main(["recurrence", "list"]) == 0
    listed = capsys.readouterr()
    assert f"id={recurrence_id}" in listed.out
    assert "status=ENABLED" in listed.out
    assert "last_outcome=-" in listed.out
    assert main(["recurrence", "show", recurrence_id]) == 0
    shown = capsys.readouterr()
    for output in (created.out, listed.out, shown.out):
        assert query_sentinel not in output
        assert str(source) not in output
        assert "workflow.json" not in output
        assert "credential_ref" not in output


def test_cli_recurrence_disable_and_run_interrupt_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()
    source = _write_workflow(tmp_path / "workflow.json", [{"action": "quota"}])
    assert main(["recurrence", "create", str(source), "--every-seconds", "300"]) == 0
    created = capsys.readouterr().out
    recurrence_id = re.search(r"id=([0-9a-f-]{36})", created)
    assert recurrence_id is not None
    value = recurrence_id.group(1)

    assert main(["recurrence", "disable", value]) == 0
    disabled = capsys.readouterr()
    assert disabled.out == f"recurrence disabled id={value}\n"
    assert disabled.err == ""
    assert main(["recurrence", "run", value]) == 1
    rejected = capsys.readouterr()
    assert rejected.out == ""
    assert rejected.err == "ERROR RECURRENCE_DISABLED\n"


def test_cli_recurrence_run_handles_keyboard_interrupt_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()
    source = _write_workflow(tmp_path / "workflow.json", [{"action": "quota"}])
    assert main(["recurrence", "create", str(source), "--every-seconds", "300"]) == 0
    created = capsys.readouterr().out
    recurrence_id = re.search(r"id=([0-9a-f-]{36})", created)
    assert recurrence_id is not None
    value = recurrence_id.group(1)

    async def interrupt(
        _runner: object,
        _recurrence_id: str,
        _execute_once: object,
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.LocalRecurrenceRunner, "run", interrupt)
    assert main(["recurrence", "run", value]) == 130
    captured = capsys.readouterr()
    assert captured.out == f"recurrence starting id={value}\n"
    assert captured.err == "ERROR INTERRUPTED\n"
