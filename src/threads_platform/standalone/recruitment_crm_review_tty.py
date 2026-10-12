"""Injected private-TTY review for the in-memory CRM snapshot contract.

This adapter displays a bounded review and checks an exact confirmation. It
does not authenticate an operator, redeem an approval, write a file, or call a
CRM. Its only successful result is ``SNAPSHOT_MATCH_ONLY``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Literal, NoReturn, TextIO

from threads_platform.standalone.manual_outcomes import ManualOutcomeRecord
from threads_platform.standalone.opportunity_queue import OpportunityQueue
from threads_platform.standalone.recruitment_crm_export import RecruitmentExportApproval
from threads_platform.standalone.recruitment_crm_review import (
    CrmExportReviewError,
    CrmExportReviewPacketV1,
    build_crm_export_review_packet,
    preflight_crm_export_review_confirmation,
    required_crm_export_confirmation,
)

SnapshotMatchOnly = Literal["SNAPSHOT_MATCH_ONLY"]

_CI_VARIABLES = (
    "CI",
    "GITHUB_ACTIONS",
    "TF_BUILD",
    "BUILD_BUILDID",
    "TEAMCITY_VERSION",
    "JENKINS_URL",
    "GITLAB_CI",
    "CIRCLECI",
    "TRAVIS",
    "BUILDKITE",
    "CODEBUILD_BUILD_ID",
    "BITBUCKET_BUILD_NUMBER",
)
_MAX_CONFIRMATION_CHARS = 108
_MAX_REVIEW_CHARS = 64 * 1024
_MAX_REVIEW_LINES = 220
_MAX_FIELD_CHARS = 256
_MAX_TARGET_BYTES = 2_048


class CrmReviewTtyError(ValueError):
    """Fixed-code failure that never includes reviewed or operator data."""

    _CODES = frozenset(
        {
            "CRM_REVIEW_TTY_REQUIRED",
            "CRM_REVIEW_TTY_CI_FORBIDDEN",
            "CRM_REVIEW_TTY_STALE",
            "CRM_REVIEW_TTY_CONFIRMATION_MISMATCH",
            "CRM_REVIEW_TTY_CONFIRMATION_REQUIRED",
            "CRM_REVIEW_TTY_INTERRUPTED",
            "CRM_REVIEW_TTY_UNSAFE_VALUE",
            "CRM_REVIEW_TTY_RENDER_LIMIT_EXCEEDED",
            "CRM_REVIEW_TTY_IO_ERROR",
            "CRM_REVIEW_TTY_INVALID_INPUT",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported private CRM review TTY error code")
        self.code = code
        super().__init__(code)


def run_crm_review_on_private_tty(
    packet: CrmExportReviewPacketV1,
    queue: OpportunityQueue,
    full_s4_records: dict[str, ManualOutcomeRecord | None],
    exact_approvals: tuple[RecruitmentExportApproval, ...],
    selected_opportunity_ids: tuple[str, ...],
    destination_identifier: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
    now_utc: str,
) -> SnapshotMatchOnly:
    """Review one exact snapshot on injected private TTY streams.

    The supplied clock and state are caller-provided and are not trusted or
    reread here. The helper validates snapshot equality before displaying any
    source-derived value and again after the operator confirms.
    """

    _require_private_tty(stdin, stdout, stderr, environment)

    try:
        confirmation = required_crm_export_confirmation(packet)
        rebuilt = build_crm_export_review_packet(
            queue,
            full_s4_records,
            selected_opportunity_ids,
            exact_approvals,
            destination_identifier=destination_identifier,
            now_utc=packet.created_at_utc,
        )
    except CrmExportReviewError:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_STALE") from None
    except Exception:  # noqa: BLE001 — malformed caller data must fail closed.
        raise CrmReviewTtyError("CRM_REVIEW_TTY_INVALID_INPUT") from None

    if rebuilt != packet:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_STALE")
    _preflight(
        packet,
        queue,
        full_s4_records,
        exact_approvals,
        selected_opportunity_ids,
        destination_identifier,
        confirmation,
        now_utc,
    )

    review = _render_review(packet, queue, confirmation)
    try:
        stdout.write(review)
        stdout.flush()
    except KeyboardInterrupt:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_INTERRUPTED") from None
    except Exception:  # noqa: BLE001 — stream implementations are caller supplied.
        raise CrmReviewTtyError("CRM_REVIEW_TTY_IO_ERROR") from None

    response = _read_confirmation(stdin)
    return _preflight(
        packet,
        queue,
        full_s4_records,
        exact_approvals,
        selected_opportunity_ids,
        destination_identifier,
        response,
        now_utc,
    )


def _require_private_tty(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
) -> None:
    try:
        for name in _CI_VARIABLES:
            value = environment.get(name, "")
            if type(value) is not str:
                raise CrmReviewTtyError("CRM_REVIEW_TTY_INVALID_INPUT")
            if value.strip():
                raise CrmReviewTtyError("CRM_REVIEW_TTY_CI_FORBIDDEN")
        if any(stream.isatty() is not True for stream in (stdin, stdout, stderr)):
            raise CrmReviewTtyError("CRM_REVIEW_TTY_REQUIRED")
    except CrmReviewTtyError:
        raise
    except KeyboardInterrupt:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_INTERRUPTED") from None
    except Exception:  # noqa: BLE001 — stream/environment failures fail closed.
        raise CrmReviewTtyError("CRM_REVIEW_TTY_REQUIRED") from None


def _preflight(
    packet: CrmExportReviewPacketV1,
    queue: OpportunityQueue,
    full_s4_records: dict[str, ManualOutcomeRecord | None],
    exact_approvals: tuple[RecruitmentExportApproval, ...],
    selected_opportunity_ids: tuple[str, ...],
    destination_identifier: str,
    confirmation: str,
    now_utc: str,
) -> SnapshotMatchOnly:
    try:
        return preflight_crm_export_review_confirmation(
            packet,
            queue,
            full_s4_records,
            exact_approvals,
            selected_opportunity_ids,
            destination_identifier,
            confirmation,
            now_utc,
        )
    except CrmExportReviewError as error:
        _raise_review_error(error)
    except Exception:  # noqa: BLE001 — malformed caller data must fail closed.
        raise CrmReviewTtyError("CRM_REVIEW_TTY_INVALID_INPUT") from None


def _raise_review_error(error: CrmExportReviewError) -> NoReturn:
    if error.code == "CRM_REVIEW_STALE":
        raise CrmReviewTtyError("CRM_REVIEW_TTY_STALE") from None
    if error.code == "CRM_REVIEW_CONFIRMATION_MISMATCH":
        raise CrmReviewTtyError("CRM_REVIEW_TTY_CONFIRMATION_MISMATCH") from None
    raise CrmReviewTtyError("CRM_REVIEW_TTY_INVALID_INPUT") from None


def _render_review(
    packet: CrmExportReviewPacketV1,
    queue: OpportunityQueue,
    confirmation: str,
) -> str:
    entries_by_id = {entry.id: entry for entry in queue.opportunities}
    lines = [
        "PRIVATE CRM REVIEW ONLY — NO FILE OR CRM ACTION",
        f"Account UUID: {_safe_value(packet.account_uuid, _MAX_FIELD_CHARS)}",
        "Action: EXPORT",
        f"Opaque destination: {_safe_value(packet.destination_identifier, _MAX_FIELD_CHARS)}",
        "Selected opportunities (in approved order):",
    ]
    for position, item in enumerate(packet.items, start=1):
        entry = entries_by_id.get(item.opportunity_id)
        if entry is None:
            raise CrmReviewTtyError("CRM_REVIEW_TTY_STALE")
        target = _safe_value(entry.target_url, _MAX_TARGET_BYTES)
        fields = ", ".join(field.value for field in item.selected_fields)
        url_choice = "OPTED_IN" if item.target_url_opt_in else "NOT_OPTED_IN"
        lines.extend(
            (
                f"[{position}] Opportunity ID: {_safe_value(item.opportunity_id, _MAX_FIELD_CHARS)}",
                f"    Target URL (private identity review only): {target}",
                f"    Purpose: {_safe_value(item.purpose.value, _MAX_FIELD_CHARS)}",
                f"    Approved export fields: {_safe_value(fields, _MAX_FIELD_CHARS)}",
                f"    TARGET_URL export choice: {url_choice}",
                f"    Lawful basis asserted: YES ({item.assertion_status})",
                f"    Rights asserted: YES ({item.assertion_status})",
                f"    Consent asserted: YES ({item.assertion_status})",
            )
        )
    lines.extend(
        (
            "Rights and consent values are operator assertions, not independently verified.",
            "Target URLs above are shown only to review identity; this screen does not export them.",
            f"Type exactly to confirm: {_safe_value(confirmation, _MAX_FIELD_CHARS)}",
        )
    )
    rendered = "\n".join(lines) + "\n"
    if len(lines) > _MAX_REVIEW_LINES or len(rendered) > _MAX_REVIEW_CHARS:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_RENDER_LIMIT_EXCEEDED")
    return rendered


def _safe_value(value: str, max_utf8_bytes: int) -> str:
    if type(value) is not str:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_UNSAFE_VALUE")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_UNSAFE_VALUE") from None
    if (
        len(encoded) > max_utf8_bytes
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(char) in {"Cf", "Co", "Cn", "Cs"} for char in value)
    ):
        raise CrmReviewTtyError("CRM_REVIEW_TTY_UNSAFE_VALUE")

    escaped: list[str] = []
    for char in value:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if char == "\\":
            escaped.append("\\\\")
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif category in {"Cc", "Zl", "Zp"} or (category == "Zs" and char != " "):
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(char)
    result = "".join(escaped)
    if len(result) > max_utf8_bytes * 4:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_UNSAFE_VALUE")
    return result


def _read_confirmation(stdin: TextIO) -> str:
    try:
        line = stdin.readline(_MAX_CONFIRMATION_CHARS + 2)
    except KeyboardInterrupt:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_INTERRUPTED") from None
    except Exception:  # noqa: BLE001 — caller supplied stream failures are redacted.
        raise CrmReviewTtyError("CRM_REVIEW_TTY_IO_ERROR") from None
    if line == "":
        raise CrmReviewTtyError("CRM_REVIEW_TTY_CONFIRMATION_REQUIRED")
    if type(line) is not str or len(line) > _MAX_CONFIRMATION_CHARS + 2 or not line.endswith("\n"):
        raise CrmReviewTtyError("CRM_REVIEW_TTY_CONFIRMATION_MISMATCH")
    response = line[:-2] if line.endswith("\r\n") else line[:-1]
    if len(response) > _MAX_CONFIRMATION_CHARS:
        raise CrmReviewTtyError("CRM_REVIEW_TTY_CONFIRMATION_MISMATCH")
    return response


__all__ = [
    "CrmReviewTtyError",
    "run_crm_review_on_private_tty",
]
