from __future__ import annotations

import ast
import re
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from threads_platform.standalone import __main__ as cli_main
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.recruitment_content_calendar import (
    render_recruitment_content_calendar,
)
from threads_platform.standalone.recruitment_content_planner import (
    ContentAttachmentMetadataV1,
    RecruitmentContentCalendarV1,
    RecruitmentContentPacketV1,
    RecruitmentContentPlannerError,
    RecruitmentContentPlannerStore,
)


def _packet(
    account_id: UUID,
    *,
    packet_id: UUID | None = None,
    scheduled_at: datetime | None = None,
    status: str = "DRAFT",
    text: str = "SYNTHETIC_POST_TEXT_SENTINEL",
    notes: str | None = "SYNTHETIC_PRIVATE_NOTE_SENTINEL",
    attachment: ContentAttachmentMetadataV1 | None = None,
) -> RecruitmentContentPacketV1:
    created_at = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    is_draft = status == "DRAFT"
    return RecruitmentContentPacketV1(
        version=1,
        account_uuid=account_id,
        packet_id=packet_id or uuid4(),
        original_text=text,
        content_provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
        rights_assertion_status="UNVERIFIED_OPERATOR_ASSERTION",
        platform_permission_status="NOT_ESTABLISHED",
        attachment=attachment,
        private_notes=notes,
        scheduled_at=scheduled_at or datetime(2026, 10, 13, 9, 30, 15, 123456, tzinfo=UTC),
        created_at=created_at,
        updated_at=created_at,
        editorial_status=status,  # type: ignore[arg-type]
        reviewed_at=None if is_draft else created_at,
        review_note=None if status != "REJECTED" else "SYNTHETIC_REVIEW_NOTE_SENTINEL",
        remote_publication_status="NOT_PUBLISHED_BY_PLANNER",
    )


def _calendar(
    *packets: RecruitmentContentPacketV1,
    account_id: UUID | None = None,
) -> RecruitmentContentCalendarV1:
    account = account_id or (packets[0].account_uuid if packets else uuid4())
    return RecruitmentContentCalendarV1(account_uuid=account, packets=packets)


def _events(ics: str) -> list[str]:
    return [
        block.split("END:VEVENT\r\n", maxsplit=1)[0] for block in ics.split("BEGIN:VEVENT\r\n")[1:]
    ]


def test_empty_calendar_has_valid_crlf_vcalendar_without_events() -> None:
    ics = render_recruitment_content_calendar(_calendar())

    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" not in ics
    assert "\n" not in ics.replace("\r\n", "")


def test_forty_packets_render_with_deterministic_order_and_bounded_lines() -> None:
    account = uuid4()
    packets = tuple(
        _packet(
            account,
            packet_id=uuid4(),
            scheduled_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC) + timedelta(minutes=index),
        )
        for index in range(40)
    )

    first = render_recruitment_content_calendar(_calendar(*packets))
    second = render_recruitment_content_calendar(_calendar(*reversed(packets)))

    assert first == second
    assert len(_events(first)) == 40
    assert all(len(line.encode("utf-8")) <= 75 for line in first.split("\r\n") if line)
    assert any(line.startswith(" ") for line in first.split("\r\n"))
    uids = [
        line[4:] for line in first.replace("\r\n ", "").split("\r\n") if line.startswith("UID:")
    ]
    assert len(uids) == len(set(uids)) == 40


def test_excess_packet_count_fails_with_fixed_error() -> None:
    account = uuid4()
    packets = tuple(_packet(account) for _ in range(41))

    with pytest.raises(RecruitmentContentPlannerError) as error:
        render_recruitment_content_calendar(_calendar(*packets))

    assert error.value.code == "CONTENT_PLANNER_LIMIT_REACHED"
    assert str(error.value) == "CONTENT_PLANNER_LIMIT_REACHED"


def test_status_descriptions_are_fixed_and_rejected_packets_are_excluded() -> None:
    account = uuid4()
    draft = _packet(account, status="DRAFT")
    ready = _packet(account, status="READY_FOR_MANUAL_PUBLICATION")
    rejected = _packet(account, status="REJECTED")

    ics = render_recruitment_content_calendar(_calendar(draft, ready, rejected))

    assert len(_events(ics)) == 2
    assert "SUMMARY:Local recruitment content reminder (DRAFT)" in ics
    assert "SUMMARY:Local recruitment content reminder (READY_FOR_MANUAL_PUBLICATION)" in ics
    unfolded = ics.replace("\r\n ", "")
    assert "DESCRIPTION:NOT_PUBLISHED_BY_PLANNER." in ics
    assert "Status: DRAFT." in unfolded
    assert "Status: READY_FOR_MANUAL_PUBLICATION." in unfolded
    assert "SYNTHETIC_REVIEW_NOTE_SENTINEL" not in ics
    assert "REJECTED" not in ics


def test_export_uses_utc_and_stable_opaque_account_scoped_identifiers() -> None:
    account_a = uuid4()
    account_b = uuid4()
    packet_id = uuid4()
    scheduled = datetime(2026, 10, 13, 9, 30, 15, 987654, tzinfo=UTC)
    first = _packet(account_a, packet_id=packet_id, scheduled_at=scheduled)
    second = replace(first, account_uuid=account_b)

    ics_a = render_recruitment_content_calendar(_calendar(first))
    ics_b = render_recruitment_content_calendar(_calendar(second))
    uid_a = next(
        line for line in ics_a.replace("\r\n ", "").split("\r\n") if line.startswith("UID:")
    )
    uid_b = next(
        line for line in ics_b.replace("\r\n ", "").split("\r\n") if line.startswith("UID:")
    )

    repeated_ics = render_recruitment_content_calendar(_calendar(first))
    repeated_uid = next(
        line for line in repeated_ics.replace("\r\n ", "").split("\r\n") if line.startswith("UID:")
    )
    assert uid_a == repeated_uid
    assert uid_a != uid_b
    assert re.fullmatch(r"UID:[0-9a-f]{64}@threads-standalone\.invalid", uid_a)
    assert str(account_a) not in ics_a
    assert str(packet_id) not in ics_a
    assert "DTSTART:20261013T093015Z" in ics_a
    assert "DTEND:20261013T094515Z" in ics_a
    assert "DTSTAMP:20261012T080000Z" in ics_a


def test_post_text_notes_urls_and_personal_metadata_never_enter_export() -> None:
    account = uuid4()
    packet_id = uuid4()
    attachment = ContentAttachmentMetadataV1(
        kind="REFERENCE_LINK",
        url="https://private.example.invalid/SYNTHETIC_URL_SENTINEL",
        label="SYNTHETIC_LABEL_SENTINEL",
        attribution="SYNTHETIC_ATTRIBUTION_SENTINEL",
        rights_asserted=True,
        consent_asserted=True,
    )
    packet = _packet(account, packet_id=packet_id, attachment=attachment)

    ics = render_recruitment_content_calendar(_calendar(packet))
    unfolded = ics.replace("\r\n ", "")

    for secret in (
        "SYNTHETIC_POST_TEXT_SENTINEL",
        "SYNTHETIC_PRIVATE_NOTE_SENTINEL",
        "SYNTHETIC_URL_SENTINEL",
        "SYNTHETIC_LABEL_SENTINEL",
        "SYNTHETIC_ATTRIBUTION_SENTINEL",
        str(account),
        str(packet_id),
        "private.example.invalid",
    ):
        assert secret not in unfolded
    assert "NOT_PUBLISHED_BY_PLANNER" in ics
    assert "Threads publication" in unfolded


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (lambda packet: replace(packet, version=True), "CONTENT_PLANNER_STATE_INVALID"),
        (
            lambda packet: replace(packet, editorial_status="PUBLISHED"),
            "CONTENT_PLANNER_STATE_INVALID",
        ),
        (
            lambda packet: replace(packet, content_provenance="BROWSER_UI"),
            "CONTENT_PLANNER_STATE_INVALID",
        ),
        (
            lambda packet: replace(
                packet,
                scheduled_at=datetime(2026, 10, 13, 9, tzinfo=timezone(timedelta(hours=3))),
            ),
            "CONTENT_PLANNER_STATE_INVALID",
        ),
        (
            lambda packet: replace(packet, scheduled_at=datetime(2026, 10, 13, 9)),
            "CONTENT_PLANNER_STATE_INVALID",
        ),
        (
            lambda packet: replace(packet, remote_publication_status="PUBLISHED"),
            "CONTENT_PLANNER_STATE_INVALID",
        ),
    ),
)
def test_invalid_packet_fields_fail_with_fixed_redacted_error(mutate, code: str) -> None:
    packet = mutate(_packet(uuid4()))

    with pytest.raises(RecruitmentContentPlannerError) as error:
        render_recruitment_content_calendar(_calendar(packet))

    assert error.value.code == code
    assert "SYNTHETIC_" not in str(error.value)


def test_wrong_account_and_duplicate_packet_identity_fail_closed() -> None:
    account = uuid4()
    packet = _packet(account)
    mismatched = replace(packet, account_uuid=uuid4())

    with pytest.raises(RecruitmentContentPlannerError) as mismatch:
        render_recruitment_content_calendar(_calendar(mismatched, account_id=account))
    assert mismatch.value.code == "CONTENT_PLANNER_ACCOUNT_MISMATCH"

    duplicate = replace(packet)
    with pytest.raises(RecruitmentContentPlannerError) as duplicate_error:
        render_recruitment_content_calendar(_calendar(packet, duplicate))
    assert duplicate_error.value.code == "CONTENT_PLANNER_STATE_INVALID"


def test_cli_parser_exposes_calendar_export_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli_main._build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["recruitment", "content-calendar-export", "--help"])

    assert error.value.code == 0
    assert "content-calendar-export [-h] alias" in capsys.readouterr().out


def test_installed_entry_path_exports_only_ics_without_network_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "private-data"
    accounts = LocalAccountStore(root)
    account = accounts.add("synthetic_calendar")
    store = RecruitmentContentPlannerStore(root)
    store.create(
        account.id,
        "SYNTHETIC_POST_TEXT_SENTINEL",
        datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
        provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
    )
    before = _tree_snapshot(root)

    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("calendar export attempted network access")

    monkeypatch.setattr(socket, "socket", reject_network)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))

    result = cli_main.main(["recruitment", "content-calendar-export", "synthetic_calendar"])
    captured = capsys.readouterr()

    assert result == 0
    assert captured.err == ""
    assert captured.out.startswith("BEGIN:VCALENDAR\r\n")
    assert captured.out.endswith("END:VCALENDAR\r\n")
    assert "SYNTHETIC_POST_TEXT_SENTINEL" not in captured.out
    assert _tree_snapshot(root) == before


def test_reader_modules_do_not_import_network_or_browser_clients() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "threads_platform" / "standalone"
    module_names = ("recruitment_content_calendar.py", "recruitment_content_cli.py")
    modules: set[str] = set()
    for name in module_names:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
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
        modules.update(imported)

    roots = {module.split(".", maxsplit=1)[0] for module in modules}
    assert not roots.intersection({"playwright", "httpx", "requests", "urllib"})
    assert not any(
        module.startswith(("threads_platform.browser", "threads_platform.api"))
        for module in modules
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
