from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import threads_platform.standalone.__main__ as cli
import threads_platform.standalone.runtime as runtime_module
from threads_platform.browser.contracts import BrowserEngineSession, BrowserLaunchRequest
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.manual_opportunities import (
    ManualOpportunityError,
    build_manual_draft_review,
    load_manual_opportunities,
)
from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeError,
    ManualOutcomeStore,
)
from threads_platform.standalone.manual_workflow import (
    ManualWorkflowError,
    run_manual_handoff_cli,
    run_manual_record_cli,
    run_manual_status_cli,
)


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeSession:
    def __init__(self) -> None:
        self.navigations: list[tuple[str, frozenset[str]]] = []
        self.thread_checks: list[tuple[str, str]] = []
        self.closed = False

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigations.append((url, allowed_origins))

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        self.thread_checks.append((target_ref, author_username))

    async def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self) -> None:
        self.requests: list[BrowserLaunchRequest] = []
        self.sessions: list[_FakeSession] = []

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        session = _FakeSession()
        self.requests.append(request)
        self.sessions.append(session)
        return session


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _target(post_id: str = "s4_fake_1") -> str:
    return f"https://www.threads.com/@synthetic/post/{post_id}"


def _fixture(tmp_path: Path) -> tuple[Path, LocalAccountStore, LocalAccount, Path, Path, Path]:
    root = tmp_path / "private-local-root"
    accounts = LocalAccountStore(root)
    account = accounts.add("synthetic_account")
    targets = _write(tmp_path / "targets.txt", _target())
    draft = _write(tmp_path / "draft.txt", "Synthetic operator-authored final draft")
    context = _write(tmp_path / "context.txt", "Synthetic operator-supplied context")
    return root, accounts, account, targets, draft, context


def _approval(
    account: LocalAccount,
    targets: Path,
    draft_text: str,
    context_text: str | None = None,
) -> str:
    candidate = load_manual_opportunities(targets).candidates[0]
    review = build_manual_draft_review(account, candidate, draft_text, context_text)
    return review.draft_sha256


def _streams(input_text: str = "") -> tuple[_TtyStringIO, _TtyStringIO, _TtyStringIO]:
    return _TtyStringIO(input_text), _TtyStringIO(), _TtyStringIO()


def test_manual_handoff_uses_exact_s3_approval_and_existing_s1_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, account, targets, draft, context = _fixture(tmp_path)
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    engine = _FakeEngine()
    prompts: list[str] = []

    async def operator_ends_session(prompt: str, *, output: io.TextIOBase | None = None) -> None:
        prompts.append(prompt)
        assert output is sys.stderr
        output.write(prompt + "\n")

    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: engine)
    monkeypatch.setattr(runtime_module, "_wait_for_interactive_operator", operator_ends_session)
    monkeypatch.setattr(cli, "_run_api_command", lambda *_args, **_kwargs: pytest.fail("API path"))
    monkeypatch.setattr(
        cli, "_run_reply_command", lambda *_args, **_kwargs: pytest.fail("reply path")
    )
    # This test supplies fake TTY streams and a fake browser engine.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    target_url = _target()
    approval_sha = _approval(
        account, targets, draft.read_text(encoding="utf-8"), "Synthetic operator-supplied context"
    )
    stdin, stdout, stderr = _streams("OPEN\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    assert (
        cli.main(
            [
                "recruitment",
                "manual-handoff",
                account.alias,
                "--targets-file",
                str(targets),
                "--select",
                "1",
                "--confirm-target",
                target_url,
                "--draft-file",
                str(draft),
                "--approve-sha",
                approval_sha,
                "--context-file",
                str(context),
                "--interactive",
            ]
        )
        == 0
    )

    assert stdout.getvalue() == ""
    rendered = stderr.getvalue()
    assert "actor identity: UNKNOWN_IDENTITY" in rendered
    assert f"exact target: {target_url}" in rendered
    assert "permalink author: synthetic (identity unverified)" in rendered
    assert "context provenance: OPERATOR_SUPPLIED" in rendered
    assert "Synthetic operator-authored final draft" in rendered
    assert f"approved draft SHA-256: {approval_sha}" in rendered
    assert "Type OPEN and press Enter" in rendered
    assert "HANDOFF_CLOSED_ACTION_UNVERIFIED" in rendered
    assert "identity=UNKNOWN_IDENTITY" in rendered
    assert len(prompts) == 1
    assert len(engine.requests) == 1
    assert engine.requests[0].headless is False
    assert engine.requests[0].profile_directory == root / "profiles" / str(account.id)
    assert engine.sessions[0].navigations == [(target_url, frozenset({"https://www.threads.com"}))]
    assert engine.sessions[0].thread_checks == [
        ("/@synthetic/post/s4_fake_1", "synthetic"),
        ("/@synthetic/post/s4_fake_1", "synthetic"),
    ]
    assert engine.sessions[0].closed

    browser_lock = FilesystemProcessLock(root / "locks" / f"{account.id}.lock")
    browser_lock.acquire()
    browser_lock.release()
    assert ManualOutcomeStore(root).read(account.id, target_url) is None


def test_manual_handoff_stops_if_account_identity_changes_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, account, targets, draft, context = _fixture(tmp_path)
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine()
    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: engine)
    approval_sha = _approval(
        account, targets, draft.read_text(encoding="utf-8"), "Synthetic operator-supplied context"
    )
    stdin, stdout, stderr = _streams("OPEN\n")

    def replace_account_before_runtime(
        data_root: Path,
        accounts: LocalAccountStore,
    ) -> runtime_module.LocalRuntime:
        account_file = data_root / "accounts" / "synthetic_account.json"
        document = json.loads(account_file.read_text(encoding="utf-8"))
        document["id"] = "f326f734-6b69-4d9e-a85f-6e7b0f8d4143"
        account_file.write_text(json.dumps(document), encoding="utf-8")
        return runtime_module.LocalRuntime(data_root, accounts)

    with pytest.raises(ManualWorkflowError) as mismatch:
        run_manual_handoff_cli(
            account.alias,
            targets,
            1,
            _target(),
            draft,
            approval_sha,
            context,
            data_root_factory=lambda: root,
            runtime_factory=replace_account_before_runtime,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )

    assert mismatch.value.code == "ACCOUNT_IDENTITY_CHANGED"
    assert engine.requests == []
    assert stdout.getvalue() == ""
    probe = FilesystemProcessLock(root / "manual_recruitment_v1" / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("bad_sha", "MANUAL_APPROVAL_MISMATCH"),
        ("changed_alias", "MANUAL_APPROVAL_MISMATCH"),
        ("changed_draft", "MANUAL_APPROVAL_MISMATCH"),
        ("changed_target", "MANUAL_TARGET_MISMATCH"),
        ("selection_drift", "MANUAL_TARGET_MISMATCH"),
    ],
)
def test_manual_handoff_preflight_rejects_changed_approval_before_browser(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    root, accounts, account, targets, draft, context = _fixture(tmp_path)
    other_account = accounts.add("other_synthetic")
    expected_sha = _approval(account, targets, draft.read_text(encoding="utf-8"))
    alias = account.alias
    selection = 1
    confirm_target = _target()
    approve_sha = expected_sha
    draft_file = draft
    if case == "bad_sha":
        approve_sha = "0" * 64
    elif case == "changed_alias":
        alias = other_account.alias
    elif case == "changed_draft":
        draft_file = _write(tmp_path / "changed-draft.txt", "Different edited draft")
    elif case == "changed_target":
        confirm_target = _target("different_target")
    elif case == "selection_drift":
        targets = _write(tmp_path / "targets.txt", _target("different_target") + "\n" + _target())

    stdin, stdout, stderr = _streams("OPEN\n")
    runtime_calls: list[object] = []

    def unexpected_runtime(*_args: object, **_kwargs: object) -> object:
        runtime_calls.append(object())
        pytest.fail("invalid S3 approval must stop before runtime construction")

    with pytest.raises((ManualWorkflowError, ManualOpportunityError)) as error:
        run_manual_handoff_cli(
            alias,
            targets,
            selection,
            confirm_target,
            draft_file,
            approve_sha,
            context,
            data_root_factory=lambda: root,
            runtime_factory=unexpected_runtime,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
    assert error.value.code == expected_code
    assert runtime_calls == []
    assert not (root / "manual_recruitment_v1").exists()


@pytest.mark.parametrize("response", ["", "\n", "OPEN \n"])
def test_manual_handoff_requires_literal_open_and_eof_stops_before_browser(
    tmp_path: Path,
    response: str,
) -> None:
    root, _, account, targets, draft, context = _fixture(tmp_path)
    approval_sha = _approval(account, targets, draft.read_text(encoding="utf-8"))
    stdin, stdout, stderr = _streams(response)
    runtime_calls: list[bool] = []

    def unexpected_runtime(*_args: object, **_kwargs: object) -> object:
        runtime_calls.append(True)
        pytest.fail("OPEN confirmation did not match; browser must remain closed")

    with pytest.raises(ManualWorkflowError) as error:
        run_manual_handoff_cli(
            account.alias,
            targets,
            1,
            _target(),
            draft,
            approval_sha,
            context,
            data_root_factory=lambda: root,
            runtime_factory=unexpected_runtime,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
    assert error.value.code == "MANUAL_HANDOFF_NOT_CONFIRMED"
    assert runtime_calls == []
    assert stdout.getvalue() == ""
    assert not (root / "manual_recruitment_v1").exists()


def test_manual_outcome_journal_is_private_idempotent_separate_and_blocks_retry(
    tmp_path: Path,
) -> None:
    root, accounts, account, targets, draft, context = _fixture(tmp_path)
    target_url = _target()
    draft_text = draft.read_text(encoding="utf-8")
    context_text = context.read_text(encoding="utf-8")
    approval_sha = _approval(account, targets, draft_text, context_text)
    stdin, stdout, stderr = _streams("RECORD\n")

    assert (
        run_manual_record_cli(
            account.alias,
            targets,
            1,
            target_url,
            draft,
            approval_sha,
            "OPERATOR_REPORTED_SENT_UNVERIFIED",
            context,
            data_root_factory=lambda: root,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
        == 0
    )
    record_store = ManualOutcomeStore(root)
    record = record_store.read(account.id, target_url)
    assert record is not None
    assert len(record.events) == 1
    assert record.latest.operator_declaration == "OPERATOR_REPORTED_SENT_UNVERIFIED"
    assert record.latest.verification == "UNVERIFIED"

    record_files = tuple((root / "manual_recruitment_v1" / "records").rglob("*.json"))
    assert len(record_files) == 1
    raw_record = record_files[0].read_bytes()
    assert target_url.encode() not in raw_record
    assert draft_text.encode() not in raw_record
    assert context_text.encode() not in raw_record
    assert record.target_fingerprint.encode() in raw_record
    assert approval_sha.encode() in raw_record

    second_input, second_stdout, second_stderr = _streams("RECORD\n")
    assert (
        run_manual_record_cli(
            account.alias,
            targets,
            1,
            target_url,
            draft,
            approval_sha,
            "OPERATOR_REPORTED_SENT_UNVERIFIED",
            context,
            data_root_factory=lambda: root,
            stdin=second_input,
            stdout=second_stdout,
            stderr=second_stderr,
            environment={},
        )
        == 0
    )
    assert "idempotent=true" in second_stderr.getvalue()
    assert len(record_store.read(account.id, target_url).events) == 1  # type: ignore[union-attr]

    other_account = accounts.add("other_synthetic")
    assert record_store.read(other_account.id, target_url) is None
    other_event, already_recorded = record_store.record(
        other_account.id,
        target_url,
        approval_sha,
        "ABORTED_NOT_SENT",
    )
    assert not already_recorded
    assert other_event.verification == "UNVERIFIED"
    assert len(tuple((root / "manual_recruitment_v1" / "records").rglob("*.json"))) == 2

    status_input, status_stdout, status_stderr = _streams()
    before_status = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert (
        run_manual_status_cli(
            account.alias,
            targets,
            1,
            target_url,
            data_root_factory=lambda: root,
            stdin=status_input,
            stdout=status_stdout,
            stderr=status_stderr,
            environment={},
        )
        == 0
    )
    status_output = status_stderr.getvalue()
    assert "status=OPERATOR_REPORTED_SENT_UNVERIFIED verification=UNVERIFIED" in status_output
    assert target_url not in status_output
    assert draft_text not in status_output
    assert status_stdout.getvalue() == ""
    assert before_status == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    handoff_input, handoff_stdout, handoff_stderr = _streams("OPEN\n")
    runtime_calls: list[bool] = []

    def unexpected_runtime(*_args: object, **_kwargs: object) -> object:
        runtime_calls.append(True)
        pytest.fail("reported-sent target must not reopen through S4")

    with pytest.raises(ManualOutcomeError) as blocked:
        run_manual_handoff_cli(
            account.alias,
            targets,
            1,
            target_url,
            draft,
            approval_sha,
            context,
            data_root_factory=lambda: root,
            runtime_factory=unexpected_runtime,
            stdin=handoff_input,
            stdout=handoff_stdout,
            stderr=handoff_stderr,
            environment={},
        )
    assert blocked.value.code == "MANUAL_OUTCOME_BLOCKED"
    assert runtime_calls == []
    assert handoff_stdout.getvalue() == ""


def test_manual_outcome_ambiguous_blocks_all_later_declarations(tmp_path: Path) -> None:
    root, _, account, _, _, _ = _fixture(tmp_path)
    store = ManualOutcomeStore(root)
    target_url = _target()
    first_sha = "1" * 64
    second_sha = "2" * 64

    _, idempotent = store.record(account.id, target_url, first_sha, "ABORTED_NOT_SENT")
    assert not idempotent
    ambiguous, idempotent = store.record(account.id, target_url, second_sha, "AMBIGUOUS")
    assert not idempotent
    assert ambiguous.verification == "UNVERIFIED"
    repeated, idempotent = store.record(account.id, target_url, second_sha, "AMBIGUOUS")
    assert idempotent
    assert repeated.event_id == ambiguous.event_id
    with pytest.raises(ManualOutcomeError) as blocked:
        store.record(account.id, target_url, first_sha, "ABORTED_NOT_SENT")
    assert blocked.value.code == "MANUAL_OUTCOME_BLOCKED"


def test_manual_outcome_unknown_version_and_symlink_fail_closed(tmp_path: Path) -> None:
    root, _, account, _, _, _ = _fixture(tmp_path)
    store = ManualOutcomeStore(root)
    target_url = _target()
    event, _ = store.record(account.id, target_url, "a" * 64, "ABORTED_NOT_SENT")
    del event
    record_file = next((root / "manual_recruitment_v1" / "records").rglob("*.json"))
    document = json.loads(record_file.read_text(encoding="utf-8"))
    document["version"] = 2
    record_file.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ManualOutcomeError) as unsupported:
        store.read(account.id, target_url)
    assert unsupported.value.code == "MANUAL_OUTCOME_VERSION_UNSUPPORTED"

    symlink_root = tmp_path / "symlink-root"
    accounts = LocalAccountStore(symlink_root)
    symlink_account = accounts.add("symlink_account")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (symlink_root / "manual_recruitment_v1").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available on this host")
    with pytest.raises(ManualOutcomeError) as linked:
        ManualOutcomeStore(symlink_root).read(symlink_account.id, target_url)
    assert linked.value.code == "MANUAL_OUTCOME_INVALID"


def test_manual_outcome_lock_is_account_keyed_and_busy_fails_closed(tmp_path: Path) -> None:
    root, _, account, _, _, _ = _fixture(tmp_path)
    store = ManualOutcomeStore(root)
    with store.account_lock(account.id), pytest.raises(ManualOutcomeError) as busy:
        store.record(account.id, _target(), "b" * 64, "ABORTED_NOT_SENT")
    assert busy.value.code == "MANUAL_OUTCOME_BUSY"


def test_s4_terminal_gate_precedes_root_and_operator_file_access(tmp_path: Path) -> None:
    _, _, account, _, _, _ = _fixture(tmp_path)

    def unexpected_root() -> Path:
        pytest.fail("private TTY check must happen before root resolution")

    non_tty = io.StringIO()
    stdout = _TtyStringIO()
    stderr = _TtyStringIO()
    calls: tuple[Callable[[], int], ...] = (
        lambda: run_manual_handoff_cli(
            account.alias,
            tmp_path / "missing-targets.txt",
            1,
            _target(),
            tmp_path / "missing-draft.txt",
            "0" * 64,
            None,
            data_root_factory=unexpected_root,
            stdin=non_tty,
            stdout=stdout,
            stderr=stderr,
            environment={},
        ),
        lambda: run_manual_record_cli(
            account.alias,
            tmp_path / "missing-targets.txt",
            1,
            _target(),
            tmp_path / "missing-draft.txt",
            "0" * 64,
            "AMBIGUOUS",
            None,
            data_root_factory=unexpected_root,
            stdin=non_tty,
            stdout=stdout,
            stderr=stderr,
            environment={},
        ),
        lambda: run_manual_status_cli(
            account.alias,
            tmp_path / "missing-targets.txt",
            1,
            _target(),
            data_root_factory=unexpected_root,
            stdin=non_tty,
            stdout=stdout,
            stderr=stderr,
            environment={},
        ),
    )
    for command in calls:
        with pytest.raises(ManualWorkflowError) as refused:
            command()
        assert refused.value.code == "PRIVATE_REVIEW_TERMINAL_REQUIRED"


def test_manual_workflow_parser_keeps_review_compatible_and_s4_explicit() -> None:
    parsed = cli._build_parser().parse_args(
        [
            "recruitment",
            "manual-handoff",
            "synthetic_account",
            "--targets-file",
            "targets.txt",
            "--select",
            "1",
            "--confirm-target",
            _target(),
            "--draft-file",
            "draft.txt",
            "--approve-sha",
            "a" * 64,
            "--interactive",
        ]
    )
    assert parsed.recruitment_command == "manual-handoff"
    assert parsed.interactive
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "recruitment",
                "manual-handoff",
                "synthetic_account",
                "--targets-file",
                "targets.txt",
                "--select",
                "1",
                "--confirm-target",
                _target(),
                "--draft-file",
                "draft.txt",
                "--approve-sha",
                "a" * 64,
            ]
        )
