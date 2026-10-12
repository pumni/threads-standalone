from __future__ import annotations

import ast
import socket
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from threads_platform.standalone import __main__ as cli_main
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.recruitment_content_cli import (
    RecruitmentContentCliError,
    run_recruitment_content_cli,
)
from threads_platform.standalone.recruitment_content_planner import (
    RecruitmentContentPlannerError,
    RecruitmentContentPlannerStore,
)


def test_cli_module_does_not_import_browser_api_or_media_clients() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "threads_platform"
        / "standalone"
        / "recruitment_content_cli.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    roots = {module.split(".", maxsplit=1)[0] for module in imported}
    assert not roots.intersection({"playwright", "httpx", "requests", "urllib"})
    assert not any(
        module.startswith(("threads_platform.browser", "threads_platform.api"))
        for module in imported
    )


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


class _CallbackTTY(_TTY):
    def __init__(self, value: str, callback: Callable[[], None]) -> None:
        super().__init__(value)
        self._callback = callback
        self._called = False

    def readline(self, size: int = -1) -> str:
        if not self._called:
            self._called = True
            self._callback()
        return super().readline(size)


def _fixture(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "private-data"
    root.mkdir()
    accounts = LocalAccountStore(root)
    first = accounts.add("synthetic_a")
    second = accounts.add("synthetic_b")
    return root, first.alias, second.alias


def _run(
    command: str,
    alias: str,
    root: Path,
    *,
    packet_id: str | None = None,
    text_file: Path | None = None,
    scheduled_utc: str | None = None,
    rights_asserted: bool = False,
    consent_asserted: bool = False,
    stdin: StringIO | None = None,
    stdout: StringIO | None = None,
    stderr: StringIO | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[int, StringIO, StringIO]:
    output = stdout if stdout is not None else _TTY()
    errors = stderr if stderr is not None else _TTY()
    result = run_recruitment_content_cli(
        command,
        alias,
        packet_id=packet_id,
        text_file=text_file,
        scheduled_utc=scheduled_utc,
        rights_asserted=rights_asserted,
        consent_asserted=consent_asserted,
        data_root_factory=lambda: root,
        stdin=stdin if stdin is not None else _TTY(),
        stdout=output,
        stderr=errors,
        environment=environment or {},
    )
    return result, output, errors


def _create_packet(root: Path, alias: str, text: str = "A synthetic recruitment note."):
    account = LocalAccountStore(root).get(alias)
    result = RecruitmentContentPlannerStore(root).create(
        account.id,
        text,
        datetime.now(UTC) + timedelta(days=1),
        provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
    )
    return account, result.packet


def _confirm(root: Path, alias: str, packet_id: str, decision: str) -> str:
    account = LocalAccountStore(root).get(alias)
    snapshot = RecruitmentContentPlannerStore(root).get_review_snapshot(
        account.id,
        UUID(packet_id),
    )
    return f"{decision} {account.id} {packet_id} {snapshot.fingerprint}\n"


@pytest.mark.parametrize(
    ("rights_asserted", "consent_asserted"),
    ((False, False), (False, True), (True, False)),
)
def test_content_add_requires_rights_and_consent_flags(
    tmp_path: Path,
    rights_asserted: bool,
    consent_asserted: bool,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    text_file = tmp_path / "content.txt"
    text_file.write_text("A synthetic recruitment note.", encoding="utf-8")

    with pytest.raises(RecruitmentContentPlannerError) as error:
        _run(
            "content-add",
            alias,
            root,
            text_file=text_file,
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=rights_asserted,
            consent_asserted=consent_asserted,
        )

    assert error.value.code == "CONTENT_PLANNER_RIGHTS_REQUIRED"
    assert (
        RecruitmentContentPlannerStore(root).list(LocalAccountStore(root).get(alias).id).packets
        == ()
    )


def test_content_add_reads_bounded_utf8_and_suppresses_text(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    text_file = tmp_path / "content.txt"
    content = "Original recruitment advice, written by the operator."
    text_file.write_text(content, encoding="utf-8")
    scheduled = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    output = _TTY()

    code, _, _ = _run(
        "content-add",
        alias,
        root,
        text_file=text_file,
        scheduled_utc=scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
        rights_asserted=True,
        consent_asserted=True,
        stdout=output,
    )

    assert code == 0
    assert content not in output.getvalue()
    assert "editorial_status=DRAFT" in output.getvalue()
    assert "NOT_PUBLISHED_BY_PLANNER" in output.getvalue()
    packet = (
        RecruitmentContentPlannerStore(root).list(LocalAccountStore(root).get(alias).id).packets[0]
    )
    assert packet.original_text == content


def test_content_add_rejects_oversized_and_symlinked_files(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 4_097)
    with pytest.raises(RecruitmentContentCliError) as too_large:
        _run(
            "content-add",
            alias,
            root,
            text_file=oversized,
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=True,
            consent_asserted=True,
        )
    assert too_large.value.code == "CONTENT_CLI_FILE_TOO_LARGE"

    invalid_utf8 = tmp_path / "invalid-utf8.txt"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(RecruitmentContentCliError) as invalid_encoding:
        _run(
            "content-add",
            alias,
            root,
            text_file=invalid_utf8,
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=True,
            consent_asserted=True,
        )
    assert invalid_encoding.value.code == "CONTENT_CLI_FILE_INVALID"

    source = tmp_path / "source.txt"
    source.write_text("A synthetic recruitment note.", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(source)
    except OSError, NotImplementedError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(RecruitmentContentCliError) as invalid:
        _run(
            "content-add",
            alias,
            root,
            text_file=link,
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=True,
            consent_asserted=True,
        )
    assert invalid.value.code == "CONTENT_CLI_FILE_INVALID"

    with pytest.raises(RecruitmentContentCliError) as repository_file:
        _run(
            "content-add",
            alias,
            root,
            text_file=Path(__file__).resolve(),
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=True,
            consent_asserted=True,
        )
    assert repository_file.value.code == "CONTENT_CLI_FILE_INVALID"

    private_root_file = root / "planner-data.txt"
    private_root_file.write_text("A synthetic recruitment note.", encoding="utf-8")
    with pytest.raises(RecruitmentContentCliError) as data_root_file:
        _run(
            "content-add",
            alias,
            root,
            text_file=private_root_file,
            scheduled_utc="2026-10-13T09:00:00Z",
            rights_asserted=True,
            consent_asserted=True,
        )
    assert data_root_file.value.code == "CONTENT_CLI_FILE_INVALID"


@pytest.mark.parametrize(
    "timestamp",
    ("2026-10-13T09:00:00+07:00", "not-a-time", "2026-10-13T09:00:00"),
)
def test_content_add_rejects_non_utc_or_invalid_timestamp(
    tmp_path: Path,
    timestamp: str,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    text_file = tmp_path / "content.txt"
    text_file.write_text("A synthetic recruitment note.", encoding="utf-8")

    with pytest.raises(RecruitmentContentCliError) as error:
        _run(
            "content-add",
            alias,
            root,
            text_file=text_file,
            scheduled_utc=timestamp,
            rights_asserted=True,
            consent_asserted=True,
        )
    assert error.value.code == "CONTENT_CLI_TIME_INVALID"


def test_duplicate_content_is_account_local_and_does_not_print_text(tmp_path: Path) -> None:
    root, first, second = _fixture(tmp_path)
    text_file = tmp_path / "content.txt"
    content = "A synthetic recruitment note.  "
    text_file.write_text(content, encoding="utf-8")
    scheduled = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    parameters = {
        "text_file": text_file,
        "scheduled_utc": scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rights_asserted": True,
        "consent_asserted": True,
    }

    _run("content-add", first, root, **parameters)
    duplicate_code, duplicate_output, _ = _run("content-add", first, root, **parameters)
    second_code, second_output, _ = _run("content-add", second, root, **parameters)

    assert duplicate_code == second_code == 0
    assert "unchanged" in duplicate_output.getvalue()
    assert "EXACT_DUPLICATE" in duplicate_output.getvalue()
    assert content not in duplicate_output.getvalue()
    assert "created" in second_output.getvalue()
    for alias in (first, second):
        account = LocalAccountStore(root).get(alias)
        assert len(RecruitmentContentPlannerStore(root).list(account.id).packets) == 1


def test_list_due_upcoming_and_show_keep_private_fields_off_generic_output(
    tmp_path: Path,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias, "Private local original text.")
    list_code, list_output, _ = _run("content-list", alias, root)
    due_code, due_output, _ = _run("content-due", alias, root)
    upcoming_code, upcoming_output, _ = _run("content-upcoming", alias, root)

    assert list_code == due_code == upcoming_code == 0
    assert packet.original_text not in list_output.getvalue()
    assert packet.original_text not in due_output.getvalue()
    assert packet.original_text not in upcoming_output.getvalue()
    assert "NOT_PUBLISHED_BY_PLANNER" in list_output.getvalue()
    assert "content_count=0" in due_output.getvalue()
    assert str(packet.packet_id) in upcoming_output.getvalue()

    show_code, show_output, show_error = _run(
        "content-show", alias, root, packet_id=str(packet.packet_id)
    )
    assert show_code == 0
    assert packet.original_text not in show_output.getvalue()
    assert packet.original_text in show_error.getvalue()
    assert "UNVERIFIED_OPERATOR_ASSERTION" in show_error.getvalue()
    assert "NOT_PUBLISHED_BY_PLANNER" in show_error.getvalue()
    assert str(account.id) in show_error.getvalue()


def test_wrong_account_cannot_show_another_accounts_packet(tmp_path: Path) -> None:
    root, first, second = _fixture(tmp_path)
    _, packet = _create_packet(root, first, "Account A private text.")

    with pytest.raises(RecruitmentContentPlannerError) as missing:
        _run("content-show", second, root, packet_id=str(packet.packet_id))

    assert missing.value.code == "CONTENT_PLANNER_ENTRY_NOT_FOUND"


@pytest.mark.parametrize("command", ("content-show", "content-approve", "content-reject"))
def test_private_review_rejects_non_tty_and_ci_output(
    tmp_path: Path,
    command: str,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    _, packet = _create_packet(root, alias)
    with pytest.raises(RecruitmentContentCliError) as not_tty:
        _run(
            command,
            alias,
            root,
            packet_id=str(packet.packet_id),
            stdin=StringIO(),
            stdout=StringIO(),
            stderr=StringIO(),
        )
    assert not_tty.value.code == "CONTENT_CLI_PRIVATE_TTY_REQUIRED"

    error_stream = _TTY()
    with pytest.raises(RecruitmentContentCliError) as ci:
        _run(
            command,
            alias,
            root,
            packet_id=str(packet.packet_id),
            stderr=error_stream,
            environment={"GITHUB_ACTIONS": "true"},
        )
    assert ci.value.code == "CONTENT_CLI_PRIVATE_TTY_REQUIRED"
    assert error_stream.getvalue() == ""


def test_approval_uses_one_compare_and_review_call_and_never_legacy_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias)
    confirmation = _confirm(root, alias, str(packet.packet_id), "APPROVE")
    original_review = RecruitmentContentPlannerStore.review_if_unchanged
    calls = 0

    def counted_review(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_review(self, *args, **kwargs)

    def forbidden_legacy(*args, **kwargs):
        raise AssertionError("legacy review method must not be used by the CLI")

    monkeypatch.setattr(RecruitmentContentPlannerStore, "review_if_unchanged", counted_review)
    monkeypatch.setattr(RecruitmentContentPlannerStore, "approve", forbidden_legacy)
    monkeypatch.setattr(RecruitmentContentPlannerStore, "reject", forbidden_legacy)
    error_stream = _TTY()

    def assert_account_lock_is_not_held() -> None:
        with RecruitmentContentPlannerStore(root).account_lock(account.id):
            pass

    code, _, _ = _run(
        "content-approve",
        alias,
        root,
        packet_id=str(packet.packet_id),
        stdin=_CallbackTTY(confirmation, assert_account_lock_is_not_held),
        stderr=error_stream,
    )

    assert code == 0
    assert calls == 1
    assert "READY_FOR_MANUAL_PUBLICATION" in error_stream.getvalue()
    assert "NOT_PUBLISHED_BY_PLANNER" in error_stream.getvalue()
    reviewed = RecruitmentContentPlannerStore(root).get(account.id, packet.packet_id)
    assert reviewed.editorial_status == "READY_FOR_MANUAL_PUBLICATION"
    assert reviewed.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"


def test_rejection_requires_exact_action_confirmation_and_private_reason(
    tmp_path: Path,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias)
    confirmation = _confirm(root, alias, str(packet.packet_id), "REJECT")
    code, _, errors = _run(
        "content-reject",
        alias,
        root,
        packet_id=str(packet.packet_id),
        stdin=_TTY(confirmation + "Does not meet the current editorial plan.\n"),
    )

    assert code == 0
    assert "REJECTED" in errors.getvalue()
    rejected = RecruitmentContentPlannerStore(root).get(account.id, packet.packet_id)
    assert rejected.editorial_status == "REJECTED"
    assert rejected.review_note == "Does not meet the current editorial plan."
    assert rejected.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"


def test_declined_confirmation_does_not_review(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias)

    with pytest.raises(RecruitmentContentCliError) as declined:
        _run(
            "content-approve",
            alias,
            root,
            packet_id=str(packet.packet_id),
            stdin=_TTY("no\n"),
        )

    assert declined.value.code == "CONTENT_CLI_CONFIRMATION_DECLINED"
    assert (
        RecruitmentContentPlannerStore(root).get(account.id, packet.packet_id).editorial_status
        == "DRAFT"
    )


def test_stale_snapshot_fails_compare_and_review_without_cli_retry(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias)
    confirmation = _confirm(root, alias, str(packet.packet_id), "APPROVE")

    def change_packet() -> None:
        RecruitmentContentPlannerStore(root).approve(account.id, packet.packet_id)

    with pytest.raises(RecruitmentContentPlannerError) as stale:
        _run(
            "content-approve",
            alias,
            root,
            packet_id=str(packet.packet_id),
            stdin=_CallbackTTY(confirmation, change_packet),
        )

    assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert RecruitmentContentPlannerStore(root).get(
        account.id, packet.packet_id
    ).editorial_status == ("READY_FOR_MANUAL_PUBLICATION")


def test_packet_id_change_or_invalid_confirmation_is_rejected(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    _, packet = _create_packet(root, alias)
    wrong_confirmation = _confirm(root, alias, str(packet.packet_id), "APPROVE").replace(
        str(packet.packet_id), str(uuid4()), 1
    )

    with pytest.raises(RecruitmentContentCliError) as rejected:
        _run(
            "content-approve",
            alias,
            root,
            packet_id=str(packet.packet_id),
            stdin=_TTY(wrong_confirmation),
        )

    assert rejected.value.code == "CONTENT_CLI_CONFIRMATION_DECLINED"


def test_interrupt_during_review_never_calls_store_decision(tmp_path: Path) -> None:
    root, alias, _ = _fixture(tmp_path)
    account, packet = _create_packet(root, alias)

    class InterruptedTTY(_TTY):
        def readline(self, size: int = -1) -> str:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(
            "content-approve",
            alias,
            root,
            packet_id=str(packet.packet_id),
            stdin=InterruptedTTY(),
        )
    assert (
        RecruitmentContentPlannerStore(root).get(account.id, packet.packet_id).editorial_status
        == "DRAFT"
    )


def test_cli_commands_are_registered_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main(["recruitment", "--help"])
    output = capsys.readouterr().out
    assert exit_info.value.code == 0
    for command in (
        "content-add",
        "content-list",
        "content-show",
        "content-due",
        "content-upcoming",
        "content-approve",
        "content-reject",
    ):
        assert command in output


def test_local_cli_path_has_no_network_or_publication_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, alias, _ = _fixture(tmp_path)
    text_file = tmp_path / "content.txt"
    text_file.write_text("A synthetic recruitment note.", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("local content planning must not access a remote service")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    code, _, _ = _run(
        "content-add",
        alias,
        root,
        text_file=text_file,
        scheduled_utc=(datetime.now(UTC) + timedelta(days=1))
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        rights_asserted=True,
        consent_asserted=True,
    )

    assert code == 0
