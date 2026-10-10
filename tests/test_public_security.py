from __future__ import annotations

from pathlib import Path

import pytest

import threads_platform.standalone.__main__ as cli_module
from threads_platform.standalone.accounts import LocalAccountStore


@pytest.mark.parametrize(
    "contents",
    [
        (
            b'{"version":1,"version":1,"id":"00000000-0000-4000-8000-000000000001",'
            b'"alias":"synthetic_account"}'
        ),
        b'{"version":1,"id":"00000000-0000-4000-8000-000000000001",'
        b'"alias":"synthetic_account"}' + b" " * 8_192,
    ],
    ids=["duplicate-json-key", "oversized-account-state"],
)
def test_account_cli_sanitizes_corrupt_or_oversized_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contents: bytes,
) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    accounts = LocalAccountStore(tmp_path)
    accounts.add("synthetic_account")
    capsys.readouterr()
    account_path = tmp_path / "accounts" / "synthetic_account.json"
    account_path.write_bytes(contents)

    assert cli_module.main(["account", "list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_STATE_INVALID\n"
    assert str(tmp_path) not in captured.out + captured.err
