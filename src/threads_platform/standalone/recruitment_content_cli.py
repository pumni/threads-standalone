"""Private, offline CLI helpers for the local recruitment content planner."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO
from uuid import UUID

from threads_platform.standalone.accounts import (
    LocalAccountStore,
    resolve_standalone_data_root,
)
from threads_platform.standalone.recruitment_content_planner import (
    RecruitmentContentPacketV1,
    RecruitmentContentPlannerError,
    RecruitmentContentPlannerStore,
    RecruitmentContentReviewSnapshotV1,
)

_MAX_TEXT_FILE_BYTES = 4_096
_REPARSE_POINT = 0x400
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_CI_VARIABLES = ("CI", "GITHUB_ACTIONS")
_COMMANDS = frozenset(
    {
        "content-add",
        "content-list",
        "content-show",
        "content-due",
        "content-upcoming",
        "content-approve",
        "content-reject",
    }
)
_REVIEW_COMMANDS = frozenset({"content-show", "content-approve", "content-reject"})


class RecruitmentContentCliError(Exception):
    """Fixed-code CLI failure that does not expose content or local paths."""

    _CODES = frozenset(
        {
            "CONTENT_CLI_CONFIRMATION_DECLINED",
            "CONTENT_CLI_FILE_INVALID",
            "CONTENT_CLI_FILE_TOO_LARGE",
            "CONTENT_CLI_INPUT_INVALID",
            "CONTENT_CLI_PRIVATE_TTY_REQUIRED",
            "CONTENT_CLI_TIME_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported recruitment content CLI error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"RecruitmentContentCliError(code={self.code})"


def run_recruitment_content_cli(
    command: str,
    alias: str,
    *,
    packet_id: str | None = None,
    text_file: Path | None = None,
    scheduled_utc: str | None = None,
    rights_asserted: bool = False,
    consent_asserted: bool = False,
    data_root_factory: Callable[[], Path] = resolve_standalone_data_root,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run one local-only content planner command against an account alias."""

    if command not in _COMMANDS:
        raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    env = os.environ if environment is None else environment

    if command in _REVIEW_COMMANDS and not _private_terminal_available(
        stdin=input_stream,
        stdout=output_stream,
        stderr=error_stream,
        environment=env,
    ):
        raise RecruitmentContentCliError("CONTENT_CLI_PRIVATE_TTY_REQUIRED")

    root = data_root_factory()
    account = LocalAccountStore(root).get(alias)
    planner = RecruitmentContentPlannerStore(root)

    if command == "content-add":
        if not rights_asserted or not consent_asserted:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_RIGHTS_REQUIRED")
        if text_file is None or scheduled_utc is None:
            raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")
        text = _read_bounded_text_file(Path(text_file), root)
        scheduled = _parse_scheduled_utc(scheduled_utc)
        result = planner.create(
            account.id,
            text,
            scheduled,
            provenance="OPERATOR_AUTHORED",
            rights_asserted=True,
            consent_asserted=True,
        )
        packet = result.packet
        state = "created" if result.created else "unchanged"
        warning_codes = ",".join(warning.code for warning in result.warnings) or "-"
        output_stream.write(
            f"{state} packet={packet.packet_id} editorial_status={packet.editorial_status} "
            f"publication_status={packet.remote_publication_status} warnings={warning_codes}\n"
        )
        return 0

    if command == "content-list":
        packets = planner.list(account.id).packets
        _write_packet_listing(output_stream, packets)
        return 0

    if command == "content-due":
        _write_packet_listing(output_stream, planner.due(account.id))
        return 0

    if command == "content-upcoming":
        _write_packet_listing(output_stream, planner.upcoming(account.id))
        return 0

    identifier = _parse_packet_id(packet_id)
    snapshot = planner.get_review_snapshot(account.id, identifier)
    _write_private_snapshot(error_stream, snapshot)
    if command == "content-show":
        return 0

    decision: Literal["APPROVE", "REJECT"] = "APPROVE" if command == "content-approve" else "REJECT"
    confirmation = f"{decision} {account.id} {identifier} {snapshot.fingerprint}"
    error_stream.write(
        f"Type exactly this confirmation to {decision.lower()} this local packet:\n{confirmation}\n"
    )
    entered = _read_tty_line(input_stream, maximum=256)
    if entered != confirmation:
        raise RecruitmentContentCliError("CONTENT_CLI_CONFIRMATION_DECLINED")

    note: str | None = None
    if decision == "REJECT":
        error_stream.write("Enter a brief local rejection reason (maximum 500 characters):\n")
        note = _read_tty_line(input_stream, maximum=500)
        if not note:
            raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")

    reviewed = planner.review_if_unchanged(
        account.id,
        identifier,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision=decision,
        note=note,
    )
    error_stream.write(
        f"reviewed packet={reviewed.packet_id} editorial_status={reviewed.editorial_status} "
        f"publication_status={reviewed.remote_publication_status}\n"
    )
    return 0


def _private_terminal_available(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
) -> bool:
    if any(environment.get(name, "").strip() for name in _CI_VARIABLES):
        return False
    try:
        return stdin.isatty() and stdout.isatty() and stderr.isatty()
    except Exception:  # noqa: BLE001 — uncertain streams must fail closed.
        return False


def _parse_packet_id(value: str | None) -> UUID:
    if type(value) is not str:
        raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")
    try:
        identifier = UUID(value)
    except ValueError, AttributeError:
        raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID") from None
    if identifier.version != 4 or str(identifier) != value:
        raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")
    return identifier


def _parse_scheduled_utc(value: str) -> datetime:
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise RecruitmentContentCliError("CONTENT_CLI_TIME_INVALID")
    body = value[:-1]
    try:
        scheduled = datetime.fromisoformat(body + "+00:00")
    except ValueError:
        raise RecruitmentContentCliError("CONTENT_CLI_TIME_INVALID") from None
    if scheduled.tzinfo is None or scheduled.utcoffset() != UTC.utcoffset(scheduled):
        raise RecruitmentContentCliError("CONTENT_CLI_TIME_INVALID")
    return scheduled


def _read_bounded_text_file(path: Path, data_root: Path) -> str:
    try:
        absolute_path = path.expanduser().absolute()
        _reject_reparse_components(absolute_path)
        resolved_path = absolute_path.resolve(strict=True)
        if _is_checkout_path(resolved_path):
            raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID")
        try:
            resolved_path.relative_to(data_root.resolve(strict=False))
        except ValueError:
            pass
        else:
            raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved_path, flags)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID")
            if metadata.st_size > _MAX_TEXT_FILE_BYTES:
                raise RecruitmentContentCliError("CONTENT_CLI_FILE_TOO_LARGE")
            content = source.read(_MAX_TEXT_FILE_BYTES + 1)
            if len(content) > _MAX_TEXT_FILE_BYTES:
                raise RecruitmentContentCliError("CONTENT_CLI_FILE_TOO_LARGE")
        return content.decode("utf-8", errors="strict")
    except RecruitmentContentCliError:
        raise
    except OSError, RuntimeError, UnicodeError, ValueError:
        raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID") from None


def _reject_reparse_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError:
            raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise RecruitmentContentCliError("CONTENT_CLI_FILE_INVALID")


def _is_checkout_path(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "threads_platform"
        ).is_dir():
            return True
    return False


def _read_tty_line(stream: TextIO, *, maximum: int) -> str:
    value = stream.readline(maximum + 2)
    if len(value) > maximum + 1 or (value and not value.endswith("\n") and len(value) > maximum):
        raise RecruitmentContentCliError("CONTENT_CLI_INPUT_INVALID")
    return value.rstrip("\r\n")


def _write_packet_listing(
    stream: TextIO,
    packets: tuple[RecruitmentContentPacketV1, ...],
) -> None:
    stream.write(f"content_count={len(packets)}\n")
    stream.writelines(
        f"packet id={packet.packet_id} scheduled_utc={_format_datetime(packet.scheduled_at)} "
        f"editorial_status={packet.editorial_status} provenance={packet.content_provenance} "
        f"rights={packet.rights_assertion_status} platform_permission="
        f"{packet.platform_permission_status} "
        f"publication_status={packet.remote_publication_status}\n"
        for packet in packets
    )


def _write_private_snapshot(
    stream: TextIO,
    snapshot: RecruitmentContentReviewSnapshotV1,
) -> None:
    stream.write("PRIVATE LOCAL REVIEW SNAPSHOT; NO REMOTE PUBLICATION\n")
    stream.write(f"content_fingerprint={snapshot.fingerprint}\n")
    stream.write(
        json.dumps(_packet_json(snapshot.packet), ensure_ascii=False, indent=2, sort_keys=True)
    )
    stream.write("\n")


def _packet_json(packet: RecruitmentContentPacketV1) -> dict[str, object]:
    attachment = packet.attachment
    return {
        "version": packet.version,
        "account_uuid": str(packet.account_uuid),
        "packet_id": str(packet.packet_id),
        "original_text": packet.original_text,
        "content_provenance": packet.content_provenance,
        "rights_asserted": packet.rights_asserted,
        "consent_asserted": packet.consent_asserted,
        "rights_assertion_status": packet.rights_assertion_status,
        "platform_permission_status": packet.platform_permission_status,
        "attachment": (
            None
            if attachment is None
            else {
                "kind": attachment.kind,
                "url": attachment.url,
                "label": attachment.label,
                "attribution": attachment.attribution,
                "rights_asserted": attachment.rights_asserted,
                "consent_asserted": attachment.consent_asserted,
            }
        ),
        "private_notes": packet.private_notes,
        "scheduled_at": _format_datetime(packet.scheduled_at),
        "created_at": _format_datetime(packet.created_at),
        "updated_at": _format_datetime(packet.updated_at),
        "editorial_status": packet.editorial_status,
        "reviewed_at": (
            None if packet.reviewed_at is None else _format_datetime(packet.reviewed_at)
        ),
        "review_note": packet.review_note,
        "remote_publication_status": packet.remote_publication_status,
    }


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
