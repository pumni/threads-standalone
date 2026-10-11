from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pytest

from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.nurture_runner import NurtureShortlistResult
from threads_platform.standalone.recruitment_reply_proposal import (
    ReplyProposalError,
    create_human_reply_preview,
)
from threads_platform.standalone.recruitment_session import RecruitmentSession

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "demo_recruitment_journey.py"
_WATCHED_TEXT = "SYNTHETIC TARGET W: A recruiter asks how a new graduate can prepare a career CV?"
_FRESH_TEXT = (
    "SYNTHETIC TARGET F: Career question? Which interview project should an intern highlight?"
)
_FAKE_REPLY = "Which project best shows the skills you used and what you learned?"


def _load_demo_module():
    spec = importlib.util.spec_from_file_location("demo_recruitment_journey_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _setup(root: Path, **api_options: object):
    demo = _load_demo_module()
    root.mkdir()
    accounts = LocalAccountStore(root)
    account = accounts.add("demo")
    assert account.credential_ref is None
    now = datetime.now(UTC)
    api = demo._SyntheticReadOnlyApi(now, **api_options)
    timing = demo._SyntheticTiming(now)
    return demo, accounts, api, timing


def _run(
    demo,
    root: Path,
    accounts: LocalAccountStore,
    api,
    timing,
    *,
    mode: str = "scripted",
    input_stream: TextIO | None = None,
    output_stream: io.StringIO | None = None,
):
    output = output_stream if output_stream is not None else io.StringIO()
    result = demo.run_journey(
        root,
        accounts,
        api,
        timing,
        editor_mode=mode,
        input_stream=input_stream if input_stream is not None else io.StringIO(),
        output_stream=output,
    )
    return result, output.getvalue()


def test_watch_cooldown_blocks_w_and_fresh_candidate_stays_exactly_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root")
    captured_shortlists: list[NurtureShortlistResult] = []
    captured_previews = []
    original_shortlist = RecruitmentSession.shortlist
    original_create = demo.create_human_reply_preview

    async def capture_shortlist(session, *args: object, **kwargs: object):
        result = await original_shortlist(session, *args, **kwargs)  # type: ignore[arg-type]
        captured_shortlists.append(result)
        return result

    async def capture_preview(shortlist, selection, generator, **kwargs: object):
        preview = await original_create(shortlist, selection, generator, **kwargs)
        captured_previews.append((shortlist, selection, preview))
        return preview

    monkeypatch.setattr(RecruitmentSession, "shortlist", capture_shortlist)
    monkeypatch.setattr(demo, "create_human_reply_preview", capture_preview)
    result, output = _run(demo, tmp_path / "synthetic-root", accounts, api, timing)

    assert result.status == "COMPLETED"
    assert result.watch.status == "COMPLETED"
    assert result.watch.cycles == 1
    assert result.receipt_count == 1
    assert result.proposal_created
    assert "candidate=W" in output and "status=COOLDOWN action=OBSERVE_ONLY" in output
    assert "CANDIDATE_W=COOLDOWN — NO PROPOSAL" in output
    assert "SELECTED_CANDIDATE=F status=ELIGIBLE action=REVIEW_CANDIDATE" in output
    assert "PROPOSAL_STATUS=PROPOSED_FOR_HUMAN_REVIEW" in output
    assert len(captured_shortlists) == 1
    shortlist, selection, preview = captured_previews[0]
    selected = shortlist.candidates[selection - 1]
    assert selected.text == _FRESH_TEXT
    assert preview.candidate is selected
    assert preview.proposal.target_fingerprint == selected.fingerprint
    assert preview.proposal.is_bound_to(selected)
    watched_index = next(
        i for i, item in enumerate(shortlist.candidates, 1) if item.text == _WATCHED_TEXT
    )
    forbidden_generator = demo._FakeReplyGenerator()
    with pytest.raises(ReplyProposalError, match="CANDIDATE_NOT_ELIGIBLE"):
        asyncio.run(create_human_reply_preview(shortlist, watched_index, forbidden_generator))
    assert forbidden_generator.requests == []
    assert api.remote_mutations == 0


def test_displayed_fingerprint_detects_replaced_or_changed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root")
    captured_shortlists: list[NurtureShortlistResult] = []
    original_shortlist = RecruitmentSession.shortlist

    async def capture_shortlist(session, *args: object, **kwargs: object):
        result = await original_shortlist(session, *args, **kwargs)  # type: ignore[arg-type]
        captured_shortlists.append(result)
        return result

    monkeypatch.setattr(RecruitmentSession, "shortlist", capture_shortlist)
    result, _ = _run(demo, tmp_path / "synthetic-root", accounts, api, timing)
    assert result.status == "COMPLETED"
    shortlist = captured_shortlists[0]
    displayed_candidates = tuple((item, item.fingerprint) for item in shortlist.candidates)
    fresh_index = next(
        i for i, item in enumerate(shortlist.candidates, 1) if item.text == _FRESH_TEXT
    )
    fresh = shortlist.candidates[fresh_index - 1]
    changed = dataclasses.replace(fresh, fingerprint=hashlib.sha256(b"replacement").hexdigest())
    replaced_items = list(shortlist.candidates)
    replaced_items[fresh_index - 1] = changed
    replaced = dataclasses.replace(shortlist, candidates=tuple(replaced_items))

    with pytest.raises(ValueError, match="target changed"):
        demo._select_candidate(replaced, fresh_index, displayed_candidates)
    changed_target = dataclasses.replace(fresh, text=_WATCHED_TEXT)
    retargeted_items = list(shortlist.candidates)
    retargeted_items[fresh_index - 1] = changed_target
    retargeted = dataclasses.replace(shortlist, candidates=tuple(retargeted_items))
    with pytest.raises(ValueError, match="target changed"):
        demo._select_candidate(retargeted, fresh_index, displayed_candidates)
    changed_binding = list(displayed_candidates)
    changed_binding[fresh_index - 1] = (fresh, "0" * 64)
    with pytest.raises(ValueError, match="target changed"):
        demo._select_candidate(shortlist, fresh_index, tuple(changed_binding))


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"fail_preflight": True}, "WATCH_STOP"),
        ({"fail_watch_read": True}, "WATCH_STOP"),
        ({"fail_shortlist_read": True}, "SHORTLIST_STOP"),
    ],
)
def test_watch_preflight_or_fresh_shortlist_failure_stops_safely(
    tmp_path: Path,
    options: dict[str, bool],
    reason: str,
) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root", **options)
    result, output = _run(demo, tmp_path / "synthetic-root", accounts, api, timing)

    assert result.status == "STOP"
    assert result.reason == reason
    assert not result.proposal_created
    assert "remote_mutations=0" not in output  # run_journey leaves final CLI footer to main
    assert api.remote_mutations == 0
    if options.get("fail_preflight") or options.get("fail_watch_read"):
        assert api.phase == "watch"
    else:
        assert api.phase == "shortlist"


def test_no_eligible_candidate_never_calls_fake_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo, accounts, api, timing = _setup(
        tmp_path / "synthetic-root",
        include_fresh_candidate=False,
    )

    async def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("proposal generation must not run")

    monkeypatch.setattr(demo, "create_human_reply_preview", forbidden)
    result, output = _run(demo, tmp_path / "synthetic-root", accounts, api, timing)

    assert result.status == "COMPLETED"
    assert not result.proposal_created
    assert "NO_ELIGIBLE_CANDIDATE=YES" in output
    assert "NOT_APPROVED_FOR_SENDING" in output and "NOT_SENT" in output
    assert api.remote_mutations == 0


@pytest.mark.parametrize(
    ("input_text", "disposition", "expected_reply"),
    [
        ("2\nK\n", "KEPT_FOR_SESSION", _FAKE_REPLY),
        ("2\nD\n", "DISCARDED", _FAKE_REPLY),
        ("2\nE\ni dont know\nK\n", "KEPT_FOR_SESSION", "i dont know"),
    ],
)
def test_interactive_keep_discard_and_quality_advisory_are_session_only(
    tmp_path: Path,
    input_text: str,
    disposition: str,
    expected_reply: str,
) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root")
    result, output = _run(
        demo,
        tmp_path / "synthetic-root",
        accounts,
        api,
        timing,
        mode="interactive",
        input_stream=io.StringIO(input_text),
    )

    assert result.editorial_disposition == disposition
    assert expected_reply in output
    assert "APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING" in output
    assert "DELIVERY_STATUS=NOT_SENT" in output
    assert "DRAFT_PERSISTED=NO remote_mutations=0" in output
    if "i dont know" in input_text:
        assert "QUALITY_FLAGS=LOW_INFORMATION" in output
    assert result.remote_mutations == api.remote_mutations == 0
    _assert_no_reply_persisted(tmp_path / "synthetic-root")


def test_ctrl_c_and_invalid_selection_never_create_proposal(tmp_path: Path) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root")

    class _InterruptingInput(io.StringIO):
        def __init__(self) -> None:
            super().__init__("2\n")
            self._reads = 0

        def readline(self, size: int = -1) -> str:
            self._reads += 1
            if self._reads > 1:
                raise KeyboardInterrupt
            return super().readline(size)

    result, output = _run(
        demo,
        tmp_path / "synthetic-root",
        accounts,
        api,
        timing,
        mode="interactive",
        input_stream=_InterruptingInput(),
    )
    assert result.editorial_disposition == "DISCARDED"
    assert result.proposal_created
    assert "INPUT_CANCELLED=YES" in output
    assert "NOT_APPROVED_FOR_SENDING" in output and "NOT_SENT" in output
    _assert_no_reply_persisted(tmp_path / "synthetic-root")

    second_root = tmp_path / "invalid-root"
    demo, accounts, api, timing = _setup(second_root)
    result, output = _run(
        demo,
        second_root,
        accounts,
        api,
        timing,
        mode="interactive",
        input_stream=io.StringIO("invalid\n"),
    )
    assert not result.proposal_created
    assert "SELECTION_REJECTED=INVALID_SELECTION" in output
    assert "NOT_APPROVED_FOR_SENDING" in output and "NOT_SENT" in output


def test_invalid_editor_action_discards_and_does_not_persist(tmp_path: Path) -> None:
    demo, accounts, api, timing = _setup(tmp_path / "synthetic-root")
    result, output = _run(
        demo,
        tmp_path / "synthetic-root",
        accounts,
        api,
        timing,
        mode="interactive",
        input_stream=io.StringIO("2\ninvalid\n"),
    )

    assert result.proposal_created
    assert result.editorial_disposition == "DISCARDED"
    assert "INPUT_REJECTED=INVALID_ACTION" in output
    assert "NOT_APPROVED_FOR_SENDING" in output and "NOT_SENT" in output
    assert api.remote_mutations == 0
    _assert_no_reply_persisted(tmp_path / "synthetic-root")


@pytest.mark.parametrize(
    ("environment", "private_tty"),
    [
        ({"PYTEST_CURRENT_TEST": "tests/test_recruitment_journey.py::test_guard (call)"}, True),
        ({"CI": "true"}, True),
        ({"GITHUB_ACTIONS": "1"}, True),
        ({}, False),
    ],
)
def test_interactive_guard_rejects_unsafe_context_before_root_account_or_api(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    private_tty: bool,
) -> None:
    demo = _load_demo_module()

    class _PrivateTty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("interactive guard must run before setup")

    monkeypatch.setattr(demo.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(demo, "_SyntheticReadOnlyApi", forbidden)
    monkeypatch.setattr(demo, "LocalAccountStore", forbidden)
    stream = _PrivateTty if private_tty else io.StringIO
    stdout, stderr = stream(), stream()
    status = demo.main(
        ["--interactive"],
        input_stream=stream(),
        output_stream=stdout,
        error_stream=stderr,
        environment=environment,
    )
    assert status == 2
    assert stderr.getvalue() == "demo status=STOP reason=PRIVATE_TTY_REQUIRED\n"
    assert _WATCHED_TEXT not in stdout.getvalue() + stderr.getvalue()
    assert _FAKE_REPLY not in stdout.getvalue() + stderr.getvalue()


def test_one_command_scripted_demo_runs_source_only_with_fake_data() -> None:
    environment = os.environ.copy()
    environment.pop("THREADS_LOCAL_DATA_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--scripted"],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "CANDIDATE_W=COOLDOWN — NO PROPOSAL" in result.stdout
    assert "SELECTED_CANDIDATE=F status=ELIGIBLE action=REVIEW_CANDIDATE" in result.stdout
    assert "QUALITY_FLAGS=LOW_INFORMATION" in result.stdout
    assert "KEPT_FOR_SESSION" in result.stdout
    assert "NOT_APPROVED_FOR_SENDING" in result.stdout
    assert "NOT_SENT" in result.stdout
    assert "remote_mutations=0" in result.stdout
    assert len(result.stdout) <= 8_500
    assert result.stderr == ""


def _assert_no_reply_persisted(root: Path) -> None:
    private_values = (
        _FAKE_REPLY.encode(),
        b"i dont know",
        b"Which project best shows the skills you used, the choices you made, and what you learned?",
        b"PROPOSED_FOR_HUMAN_REVIEW",
        b"LOW_INFORMATION",
        b"REVIEW_RECOMMENDED",
        b"KEPT_FOR_SESSION",
        b"DISCARDED",
        b"NOT_APPROVED_FOR_SENDING",
        b"NOT_SENT",
    )
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            assert not any(value in content for value in private_values)
