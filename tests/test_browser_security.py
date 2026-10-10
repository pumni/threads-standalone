from __future__ import annotations

import builtins
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from threads_platform.api.contracts import PublishingQuota
from threads_platform.standalone.__main__ import main


class _SyntheticApiRuntime:
    async def quota(self, alias: str) -> PublishingQuota:
        assert alias == "synthetic_account"
        return PublishingQuota(usage=1, total=10, reply_usage=0, reply_total=5)


def test_account_and_api_only_cli_paths_never_import_browser_or_playwright(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    browser_imports: list[str] = []

    @asynccontextmanager
    async def fake_app(root: Path, accounts: object = None):
        del root, accounts
        yield SimpleNamespace(api=_SyntheticApiRuntime())

    monkeypatch.setattr(cli, "build_standalone_app", fake_app)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "playwright" or name.startswith(
            ("playwright.", "threads_platform.browser", "threads_platform.locks")
        ):
            browser_imports.append(name)
            raise AssertionError(f"unexpected browser import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert main(["account", "add", "synthetic_account"]) == 0
    assert main(["account", "list"]) == 0
    assert main(["api", "quota", "synthetic_account"]) == 0

    output = capsys.readouterr().out
    assert "added synthetic_account" in output
    assert "synthetic_account" in output
    assert "quota usage=1 total=10" in output
    assert browser_imports == []
