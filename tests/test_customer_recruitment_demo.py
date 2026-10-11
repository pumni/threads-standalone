from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "customer_recruitment_demo.py"


def _load_customer_demo():
    spec = importlib.util.spec_from_file_location("customer_recruitment_demo_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_one_command_launcher_runs_existing_ao05a_synthetic_journey() -> None:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "DEMO_MODE=SYNTHETIC" in result.stdout
    assert "CHROME_LOGIN=NOT_EXECUTED" in result.stdout
    assert "OAUTH_STATUS=NOT_CONNECTED" in result.stdout
    assert "LONG_LIVED_TOKEN=NOT_ACQUIRED" in result.stdout
    assert "REAL_API=DISABLED" in result.stdout
    assert "SEND=DISABLED" in result.stdout
    assert "backend=FAKE_API identity=FAKE credentials=NONE root=TEMPORARY" in result.stdout
    assert "WATCH_COMPLETED=YES" in result.stdout
    assert "candidate=W" in result.stdout and "status=COOLDOWN action=OBSERVE_ONLY" in result.stdout
    assert "SELECTED_CANDIDATE=F status=ELIGIBLE action=REVIEW_CANDIDATE" in result.stdout
    assert "PROPOSAL_STATUS=PROPOSED_FOR_HUMAN_REVIEW" in result.stdout
    assert "EDITORIAL_DISPOSITION=KEPT_FOR_SESSION" in result.stdout
    assert "APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING" in result.stdout
    assert "DELIVERY_STATUS=NOT_SENT" in result.stdout
    assert "DRAFT_PERSISTED=NO" in result.stdout
    assert "remote_mutations=0" in result.stdout
    assert "OAUTH_STATUS=CONNECTED" not in result.stdout
    assert "LONG_LIVED_TOKEN=ACQUIRED" not in result.stdout
    assert result.stderr == ""


def test_interactive_launcher_delegates_to_existing_editor_in_private_tty() -> None:
    demo = _load_customer_demo()

    class _PrivateTty(io.StringIO):
        def isatty(self) -> bool:
            return True

    output, errors = _PrivateTty(), _PrivateTty()
    status = demo.main(
        ["--interactive"],
        input_stream=_PrivateTty("2\nD\n"),
        output_stream=output,
        error_stream=errors,
        environment={},
    )

    assert status == 0
    assert "SELECTED_CANDIDATE=F status=ELIGIBLE action=REVIEW_CANDIDATE" in output.getvalue()
    assert "EDITORIAL_DISPOSITION=DISCARDED" in output.getvalue()
    assert "APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING" in output.getvalue()
    assert "DELIVERY_STATUS=NOT_SENT" in output.getvalue()
    assert "remote_mutations=0" in output.getvalue()
    assert errors.getvalue() == ""


def test_readiness_only_does_not_load_ao05a_or_create_a_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo = _load_customer_demo()

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("readiness-only must not load the AO-05A runtime")

    monkeypatch.setattr(demo.runpy, "run_path", forbidden)
    output, errors = io.StringIO(), io.StringIO()
    status = demo.main(
        ["--readiness-only"],
        input_stream=io.StringIO(),
        output_stream=output,
        error_stream=errors,
        environment={},
    )

    assert status == 0
    assert "DEMO_MODE=SYNTHETIC" in output.getvalue()
    assert (
        "Chrome account login EXISTING CLI CODE; NOT EXECUTED OR LIVE-VERIFIED" in output.getvalue()
    )
    assert "OAuth authorization/code exchange NOT IMPLEMENTED" in output.getvalue()
    assert "Long-lived token acquisition/refresh NOT IMPLEMENTED" in output.getvalue()
    assert "Live account UAT NOT AUTHORIZED" in output.getvalue()
    assert errors.getvalue() == ""


@pytest.mark.parametrize(
    ("environment", "private_tty"),
    [({"CI": "true"}, True), ({}, False)],
)
def test_interactive_refusal_precedes_synthetic_root_account_and_api(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    private_tty: bool,
) -> None:
    demo = _load_customer_demo()
    original_run_path = demo.runpy.run_path

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("interactive guard must run before local setup")

    def guarded_run_path(*args: object, **kwargs: object):
        namespace = original_run_path(*args, **kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(namespace["tempfile"], "TemporaryDirectory", forbidden)
        monkeypatch.setitem(namespace, "LocalAccountStore", forbidden)
        monkeypatch.setitem(namespace, "_SyntheticReadOnlyApi", forbidden)
        return namespace

    monkeypatch.setattr(demo.runpy, "run_path", guarded_run_path)

    class _PrivateTty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream_type = _PrivateTty if private_tty else io.StringIO
    output, errors = stream_type(), stream_type()
    status = demo.main(
        ["--interactive"],
        input_stream=stream_type(),
        output_stream=output,
        error_stream=errors,
        environment=environment,
    )

    assert status == 2
    assert "DEMO_MODE=SYNTHETIC" in output.getvalue()
    assert "JOURNEY_STATUS" not in output.getvalue()
    assert errors.getvalue() == "demo status=STOP reason=PRIVATE_TTY_REQUIRED\n"
