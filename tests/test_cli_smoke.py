from __future__ import annotations

import pytest

from threads_platform.standalone.__main__ import main


def test_help_is_available_without_runtime_capabilities(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "usage: threads-local" in capsys.readouterr().out
