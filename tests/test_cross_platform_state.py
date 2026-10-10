from __future__ import annotations

import os
from pathlib import Path

import pytest

from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.accounts import LocalAccountStore, StandaloneAccountError


def _create_symlink_or_skip_for_windows_privilege(
    link: Path,
    target: Path,
) -> None:
    try:
        link.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) in {5, 1314}:
            pytest.skip(f"Windows symlink privilege unavailable (WinError {error.winerror})")
        raise


def test_account_state_symlink_to_outside_root_fails_closed(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("synthetic_account")
    account_path = tmp_path / "accounts" / "synthetic_account.json"
    outside_path = tmp_path.parent / f"{tmp_path.name}-external-account.json"
    outside_path.write_text(
        '{"version":1,"id":"00000000-0000-4000-8000-000000000001","alias":"synthetic_account"}',
        encoding="utf-8",
    )
    account_path.unlink()
    try:
        _create_symlink_or_skip_for_windows_privilege(account_path, outside_path)
        with pytest.raises(StandaloneAccountError) as caught:
            store.get(account.alias)
        assert caught.value.code == "ACCOUNT_STATE_INVALID"
        assert str(outside_path) not in str(caught.value)
    finally:
        outside_path.unlink(missing_ok=True)


def test_filesystem_account_lock_releases_after_collision(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "synthetic-account.lock"
    owner = FilesystemProcessLock(lock_path)
    contender = FilesystemProcessLock(lock_path)
    owner.acquire()
    try:
        assert owner.held
        with pytest.raises(ProcessAlreadyRunning):
            contender.acquire()
        assert not contender.held
    finally:
        owner.release()

    contender.acquire()
    assert contender.held
    contender.release()
    assert not contender.held
