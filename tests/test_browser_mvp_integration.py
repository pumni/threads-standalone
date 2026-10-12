from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

import pytest

import threads_platform
import threads_platform.standalone.__main__ as cli
import threads_platform.standalone.runtime as runtime_module
from threads_platform.browser.contracts import BrowserEngineSession, BrowserLaunchRequest
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.manual_opportunities import (
    build_manual_draft_review,
    load_manual_opportunities,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeSession:
    def __init__(self) -> None:
        self.navigations: list[tuple[str, frozenset[str]]] = []
        self.target_checks: list[tuple[str, str]] = []
        self.closed = False

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigations.append((url, allowed_origins))

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        self.target_checks.append((target_ref, author_username))

    async def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(
        self,
        root: Path,
        account: LocalAccount,
        target_url: str,
        events: list[str],
    ) -> None:
        self.root = root
        self.account = account
        self.target_url = target_url
        self.events = events
        self.requests: list[BrowserLaunchRequest] = []
        self.sessions: list[_FakeSession] = []

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        # A new store instance sees the durable attempt before browser startup.
        record = ManualOutcomeStore(self.root).read(self.account.id, self.target_url)
        assert record is not None
        assert record.latest.status == "HANDOFF_UNRESOLVED"
        # The journal lock must have been released before LocalRuntime starts.
        with ManualOutcomeStore(self.root).account_lock(self.account.id):
            pass
        self.events.append("browser_open")
        session = _FakeSession()
        self.requests.append(request)
        self.sessions.append(session)
        return session


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _invoke_cli(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    *,
    input_text: str = "",
) -> tuple[int, str, str]:
    stdin = _TtyStringIO(input_text)
    stdout = _TtyStringIO()
    stderr = _TtyStringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    result = cli.main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


def _selected_arguments(
    command: str,
    alias: str,
    targets_file: Path,
    draft_file: Path,
    target_url: str,
    *,
    selection: int = 2,
    approval_sha: str | None = None,
    attempt_id: str | None = None,
    outcome: str | None = None,
    context_file: Path | None = None,
) -> list[str]:
    arguments = [
        "recruitment",
        command,
        alias,
        "--targets-file",
        str(targets_file),
        "--select",
        str(selection),
        "--confirm-target",
        target_url,
    ]
    if command != "manual-status":
        arguments.extend(["--draft-file", str(draft_file)])
    if context_file is not None:
        arguments.extend(["--context-file", str(context_file)])
    if approval_sha is not None:
        arguments.extend(["--approve-sha", approval_sha])
    if attempt_id is not None:
        arguments.extend(["--attempt-id", attempt_id])
    if outcome is not None:
        arguments.extend(["--outcome", outcome])
    if command == "manual-handoff":
        arguments.append("--interactive")
    return arguments


def test_browser_mvp_cli_flow_and_installed_wheel_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the complete private workflow through the installed CLI dispatcher."""
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        imported_path = Path(threads_platform.__file__).resolve()
        assert "site-packages" in str(imported_path).casefold()
        checkout = Path(__file__).resolve().parents[1]
        active_paths = {Path(item or os.getcwd()).resolve() for item in sys.path if item}
        assert checkout.resolve() not in active_paths
        assert (checkout / "src").resolve() not in active_paths

    root = tmp_path / "synthetic-data-root"
    accounts = LocalAccountStore(root)
    account = accounts.add("synthetic_account")
    other_account = accounts.add("synthetic_other_account")
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    (root / "profiles" / str(other_account.id)).mkdir(parents=True)

    first_target = "https://www.threads.com/@synthetic/post/s5_first"
    target_url = "https://www.threads.com/@synthetic/post/s5_SelectedCase_2"
    reported_target = "https://www.threads.com/@synthetic/post/s5_reported_3"
    targets_file = _write(
        tmp_path / "synthetic-targets.txt",
        "\n".join(
            (
                first_target,
                first_target + "?utm_source=synthetic",
                target_url + "?xmt=synthetic",
                reported_target,
            )
        )
        + "\n",
    )
    draft_text = "Synthetic operator-authored final draft."
    context_text = "Synthetic context written by the operator."
    draft_file = _write(tmp_path / "synthetic-draft.txt", draft_text)
    context_file = _write(tmp_path / "synthetic-context.txt", context_text)

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def unexpected_remote_runtime(*_args: object, **_kwargs: object) -> object:
        pytest.fail("the manual workflow must not invoke API, Nurture, or mutation runtimes")

    monkeypatch.setattr(cli, "_run_api_command", unexpected_remote_runtime)
    monkeypatch.setattr(cli, "_run_post_command", unexpected_remote_runtime)
    monkeypatch.setattr(cli, "_run_reply_command", unexpected_remote_runtime)
    monkeypatch.setattr(cli, "_handle_nurture_command", unexpected_remote_runtime)
    monkeypatch.setattr(cli, "_run_browser_command", unexpected_remote_runtime)

    events: list[str] = []
    engines: list[_FakeEngine] = []
    handoff_specs = [
        (account, target_url),
        (account, target_url),
        (other_account, reported_target),
    ]

    def fake_engine_factory() -> _FakeEngine:
        expected_account, expected_target = handoff_specs[len(engines)]
        engine = _FakeEngine(root, expected_account, expected_target, events)
        engines.append(engine)
        return engine

    async def operator_closes_browser(prompt: str, *, output: io.TextIOBase | None = None) -> None:
        assert output is sys.stderr
        assert "identity=UNKNOWN_IDENTITY" in prompt
        events.append("operator_closed")

    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", fake_engine_factory)
    monkeypatch.setattr(runtime_module, "_wait_for_interactive_operator", operator_closes_browser)

    list_result, list_stdout, list_stderr = _invoke_cli(
        monkeypatch,
        [
            "recruitment",
            "manual-list",
            account.alias,
            "--targets-file",
            str(targets_file),
        ],
    )
    assert list_result == 0
    assert list_stdout == ""
    assert f"1. source_line=1 target={first_target}" in list_stderr
    assert f"2. source_line=3 target={target_url}" in list_stderr
    assert f"3. source_line=4 target={reported_target}" in list_stderr
    assert "duplicate source_line=2 maps_to_selection=1 reason=DUPLICATE" in list_stderr

    review_result, review_stdout, review_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-review",
            account.alias,
            targets_file,
            draft_file,
            target_url,
            context_file=context_file,
        ),
    )
    assert review_result == 0
    assert review_stdout == ""
    assert f"selected target: {target_url}" in review_stderr
    assert "actor identity: UNKNOWN_IDENTITY" in review_stderr
    assert "author_identity=UNVERIFIED" in review_stderr
    assert draft_text in review_stderr
    approval_match = re.search(r"draft sha256: ([0-9a-f]{64})", review_stderr)
    assert approval_match is not None
    approval_sha = approval_match.group(1)
    selected = load_manual_opportunities(targets_file).candidates[1]
    expected_review = build_manual_draft_review(account, selected, draft_text, context_text)
    no_context_review = build_manual_draft_review(account, selected, draft_text, None)
    assert approval_sha == expected_review.draft_sha256 == no_context_review.draft_sha256

    handoff_args = _selected_arguments(
        "manual-handoff",
        account.alias,
        targets_file,
        draft_file,
        target_url,
        approval_sha=approval_sha,
        context_file=context_file,
    )
    handoff_result, handoff_stdout, handoff_stderr = _invoke_cli(
        monkeypatch,
        handoff_args,
        input_text="OPEN\n",
    )
    assert handoff_result == 0
    assert handoff_stdout == ""
    assert "actor identity: UNKNOWN_IDENTITY" in handoff_stderr
    assert f"exact target: {target_url}" in handoff_stderr
    assert f"approved draft SHA-256: {approval_sha}" in handoff_stderr
    assert "HANDOFF_UNRESOLVED attempt_id=" in handoff_stderr
    assert "HANDOFF_CLOSED_ACTION_UNVERIFIED" in handoff_stderr
    assert len(engines) == 1
    assert engines[0].requests[0].headless is False
    assert engines[0].requests[0].profile_directory == root / "profiles" / str(account.id)
    assert engines[0].sessions[0].navigations == [
        (target_url, frozenset({"https://www.threads.com"}))
    ]
    assert engines[0].sessions[0].target_checks == [
        ("/@synthetic/post/s5_SelectedCase_2", "synthetic"),
        ("/@synthetic/post/s5_SelectedCase_2", "synthetic"),
    ]
    assert engines[0].sessions[0].closed
    assert events == ["browser_open", "operator_closed"]

    first_attempt_match = re.search(
        r"HANDOFF_UNRESOLVED attempt_id=([0-9a-f-]{36})", handoff_stderr
    )
    assert first_attempt_match is not None
    first_attempt_id = first_attempt_match.group(1)
    # A fresh store (as after a process restart) still sees the unresolved event.
    first_record = ManualOutcomeStore(root).read(account.id, target_url)
    assert first_record is not None
    assert first_record.latest.status == "HANDOFF_UNRESOLVED"
    assert first_record.latest.attempt_id == first_attempt_id

    status_args = _selected_arguments(
        "manual-status", account.alias, targets_file, draft_file, target_url
    )
    status_result, status_stdout, status_stderr = _invoke_cli(monkeypatch, status_args)
    assert status_result == 0
    assert status_stdout == ""
    assert "status=HANDOFF_UNRESOLVED verification=UNVERIFIED" in status_stderr
    assert f"attempt_id={first_attempt_id}" in status_stderr

    other_status, _, other_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-status", other_account.alias, targets_file, draft_file, target_url
        ),
    )
    assert other_status == 0
    assert "status=NO_RECORD verification=UNVERIFIED" in other_stderr

    abort_result, abort_stdout, abort_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-record",
            account.alias,
            targets_file,
            draft_file,
            target_url,
            approval_sha=approval_sha,
            attempt_id=first_attempt_id,
            outcome="ABORTED_NOT_SENT",
            context_file=context_file,
        ),
        input_text="RECORD\n",
    )
    assert abort_result == 0
    assert abort_stdout == ""
    assert "recorded=ABORTED_NOT_SENT verification=UNVERIFIED" in abort_stderr

    second_result, _, second_stderr = _invoke_cli(
        monkeypatch,
        handoff_args,
        input_text="OPEN\n",
    )
    assert second_result == 0
    second_attempt_match = re.search(
        r"HANDOFF_UNRESOLVED attempt_id=([0-9a-f-]{36})", second_stderr
    )
    assert second_attempt_match is not None
    second_attempt_id = second_attempt_match.group(1)
    assert second_attempt_id != first_attempt_id
    assert len(engines) == 2

    stale_result, _, stale_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-record",
            account.alias,
            targets_file,
            draft_file,
            target_url,
            approval_sha=approval_sha,
            attempt_id=first_attempt_id,
            outcome="ABORTED_NOT_SENT",
            context_file=context_file,
        ),
        input_text="RECORD\n",
    )
    assert stale_result == 1
    assert "ERROR MANUAL_OUTCOME_ATTEMPT_MISMATCH" in stale_stderr
    pending_after_stale = ManualOutcomeStore(root).read(account.id, target_url)
    assert pending_after_stale is not None
    assert pending_after_stale.latest.status == "HANDOFF_UNRESOLVED"
    assert pending_after_stale.latest.attempt_id == second_attempt_id

    ambiguous_result, _, ambiguous_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-record",
            account.alias,
            targets_file,
            draft_file,
            target_url,
            approval_sha=approval_sha,
            attempt_id=second_attempt_id,
            outcome="AMBIGUOUS",
            context_file=context_file,
        ),
        input_text="RECORD\n",
    )
    assert ambiguous_result == 0
    assert "recorded=AMBIGUOUS verification=UNVERIFIED" in ambiguous_stderr

    blocked_result, _, blocked_stderr = _invoke_cli(
        monkeypatch,
        handoff_args,
        input_text="OPEN\n",
    )
    assert blocked_result == 1
    assert "ERROR MANUAL_OUTCOME_BLOCKED" in blocked_stderr
    assert len(engines) == 2

    reported_review_result, _, reported_review_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-review",
            other_account.alias,
            targets_file,
            draft_file,
            reported_target,
            selection=3,
            context_file=context_file,
        ),
    )
    assert reported_review_result == 0
    reported_sha_match = re.search(r"draft sha256: ([0-9a-f]{64})", reported_review_stderr)
    assert reported_sha_match is not None
    reported_sha = reported_sha_match.group(1)
    reported_review = build_manual_draft_review(
        other_account,
        load_manual_opportunities(targets_file).candidates[2],
        draft_text,
        context_text,
    )
    assert reported_sha == reported_review.draft_sha256

    reported_handoff_args = _selected_arguments(
        "manual-handoff",
        other_account.alias,
        targets_file,
        draft_file,
        reported_target,
        selection=3,
        approval_sha=reported_sha,
        context_file=context_file,
    )
    reported_handoff_result, _, reported_handoff_stderr = _invoke_cli(
        monkeypatch,
        reported_handoff_args,
        input_text="OPEN\n",
    )
    assert reported_handoff_result == 0
    reported_attempt_match = re.search(
        r"HANDOFF_UNRESOLVED attempt_id=([0-9a-f-]{36})", reported_handoff_stderr
    )
    assert reported_attempt_match is not None
    reported_attempt_id = reported_attempt_match.group(1)
    assert len(engines) == 3

    reported_result, _, reported_stderr = _invoke_cli(
        monkeypatch,
        _selected_arguments(
            "manual-record",
            other_account.alias,
            targets_file,
            draft_file,
            reported_target,
            selection=3,
            approval_sha=reported_sha,
            attempt_id=reported_attempt_id,
            outcome="OPERATOR_REPORTED_SENT_UNVERIFIED",
            context_file=context_file,
        ),
        input_text="RECORD\n",
    )
    assert reported_result == 0
    assert "recorded=OPERATOR_REPORTED_SENT_UNVERIFIED verification=UNVERIFIED" in reported_stderr
    reported_blocked_result, _, reported_blocked_stderr = _invoke_cli(
        monkeypatch,
        reported_handoff_args,
        input_text="OPEN\n",
    )
    assert reported_blocked_result == 1
    assert "ERROR MANUAL_OUTCOME_BLOCKED" in reported_blocked_stderr
    assert len(engines) == 3

    journal_files = tuple((root / "manual_recruitment_v1").rglob("*.json"))
    assert journal_files
    journal_content = b"\n".join(path.read_bytes() for path in journal_files)
    assert target_url.encode() not in journal_content
    assert draft_text.encode() not in journal_content
    assert context_text.encode() not in journal_content
