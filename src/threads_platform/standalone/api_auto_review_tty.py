"""Private, challenge-bound TTY review for an inert API-AUTO preview.

This module accepts only in-memory DTOs and injected terminal streams. A
successful result records a human review event in memory; it is not approval,
does not authorize dispatch, and does not persist anything.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TextIO, cast
from uuid import UUID

from threads_platform.standalone.api_auto_content import (
    ContentOrigin,
    JobFactsDeclaration,
    ReviewContentPacket,
    RightsBasis,
    RightsEvidence,
)
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    CampaignScope,
    CandidateSource,
    ObservationProvenance,
    ObservedCandidate,
)
from threads_platform.standalone.api_auto_review_context import (
    PrivateReviewDecision,
    PrivateReviewPreview,
    PrivateReviewPreviewResult,
    ReviewDisplaySnapshot,
    build_private_review_preview,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_CI_ENVIRONMENT_VARIABLES = (
    "CI",
    "GITHUB_ACTIONS",
    "TF_BUILD",
    "BUILDKITE",
    "TEAMCITY_VERSION",
    "JENKINS_URL",
)
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_OUTPUT_LINES = 220
_MAX_CONFIRMATION_READ = 128


class PrivateTtyReviewDecision(StrEnum):
    CONFIRMED_FOR_REVIEW_ONLY = "CONFIRMED_FOR_REVIEW_ONLY"
    CANCELLED = "CANCELLED"
    STOP = "STOP"


class PrivateTtyReviewReason(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    PRIVATE_TTY_REQUIRED = "PRIVATE_TTY_REQUIRED"
    CI_FORBIDDEN = "CI_FORBIDDEN"
    PREVIEW_MISMATCH = "PREVIEW_MISMATCH"
    EXPIRED = "EXPIRED"
    INVALID_VISIBLE_TEXT = "INVALID_VISIBLE_TEXT"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    IO_ERROR = "IO_ERROR"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    CLOCK_INVALID = "CLOCK_INVALID"


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTtyReviewReceipt:
    """Ephemeral confirmation evidence; never a durable approval or grant."""

    packet_revision_sha256: str = field(repr=False)
    preview_sha256: str = field(repr=False)
    account_id: UUID = field(repr=False)
    action: ActionKind
    confirmed_at: datetime

    def __repr__(self) -> str:
        action = self.action.value if type(self.action) is ActionKind else "INVALID"
        return f"PrivateTtyReviewReceipt(action={action}, private_confirmation=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class PrivateTtyReviewResult:
    decision: PrivateTtyReviewDecision
    reason_codes: tuple[PrivateTtyReviewReason, ...]
    receipt: PrivateTtyReviewReceipt | None = field(repr=False)

    def __repr__(self) -> str:
        decision = (
            self.decision.value if type(self.decision) is PrivateTtyReviewDecision else "INVALID"
        )
        reasons = (
            tuple(reason.value for reason in self.reason_codes)
            if type(self.reason_codes) is tuple
            and all(type(reason) is PrivateTtyReviewReason for reason in self.reason_codes)
            else ("INVALID",)
        )
        return (
            f"PrivateTtyReviewResult(decision={decision}, reason_codes={reasons!r}, "
            f"receipt_present={self.receipt is not None})"
        )


def run_private_review_tty(
    preview: PrivateReviewPreview,
    packet: ReviewContentPacket,
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    display: ReviewDisplaySnapshot,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
    now_utc: Callable[[], datetime],
) -> PrivateTtyReviewResult:
    """Render a complete exact preview privately and collect a bounded challenge.

    The same pure preview validator runs before rendering and after a correct
    response. The second check validates these supplied immutable DTOs; it does
    not reacquire or authenticate a live API observation.
    """

    environment_snapshot = _environment_snapshot(environment)
    if (
        type(preview) is not PrivateReviewPreview
        or type(packet) is not ReviewContentPacket
        or type(scope) is not CampaignScope
        or type(candidates) is not tuple
        or any(type(candidate) is not ObservedCandidate for candidate in candidates)
        or type(display) is not ReviewDisplaySnapshot
        or not callable(now_utc)
        or environment_snapshot is None
        or not _valid_stream_set(stdin, stdout, stderr)
    ):
        return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.INVALID_INPUT)

    try:
        if any(environment_snapshot.get(name, "") != "" for name in _CI_ENVIRONMENT_VARIABLES):
            return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.CI_FORBIDDEN)
    except Exception:  # noqa: BLE001 - untrusted injected mapping must fail closed
        return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.INVALID_INPUT)

    if not _all_streams_are_tty(stdin, stdout, stderr):
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.PRIVATE_TTY_REQUIRED,
        )

    first_now, clock_reason = _read_clock(now_utc)
    if clock_reason is not None or first_now is None:
        return _result(
            PrivateTtyReviewDecision.STOP,
            clock_reason or PrivateTtyReviewReason.CLOCK_INVALID,
        )

    reason = _revalidate_supplied_preview(
        preview,
        packet,
        scope,
        candidates,
        display,
        now=first_now,
    )
    if reason is not None:
        return _result(PrivateTtyReviewDecision.STOP, reason)

    try:
        challenge = _confirmation_challenge(preview)
        rendered = _render_preview(preview)
    except Exception:  # noqa: BLE001 - malformed visible DTOs must not escape or leak
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,
        )
    if challenge is None:
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.PREVIEW_MISMATCH,
        )
    if rendered is None:
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,
        )
    prompt = f"\nConfirmation challenge (not an approval token): {challenge}\n> "
    try:
        output_size = len((rendered + prompt).encode("utf-8", errors="strict"))
    except UnicodeError:
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,
        )
    if (
        output_size > _MAX_OUTPUT_BYTES
        or _displayed_line_count(rendered + prompt) > _MAX_OUTPUT_LINES
    ):
        return _result(
            PrivateTtyReviewDecision.STOP,
            PrivateTtyReviewReason.OUTPUT_LIMIT_EXCEEDED,
        )

    try:
        _write_complete(stderr, rendered)
        _write_complete(stderr, prompt)
        entered = stdin.readline(_MAX_CONFIRMATION_READ)
    except KeyboardInterrupt:
        return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.INTERRUPTED)
    except Exception:  # noqa: BLE001 - terminal failures must not expose private data
        return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.IO_ERROR)

    if type(entered) is not str:
        return _result(PrivateTtyReviewDecision.STOP, PrivateTtyReviewReason.IO_ERROR)
    if entered == "":
        return _result(PrivateTtyReviewDecision.CANCELLED, PrivateTtyReviewReason.CANCELLED)
    if len(entered) >= _MAX_CONFIRMATION_READ or not _exact_line(entered, challenge):
        return _result(
            PrivateTtyReviewDecision.CANCELLED,
            PrivateTtyReviewReason.CONFIRMATION_MISMATCH,
        )

    confirmed_at, clock_reason = _read_clock(now_utc)
    if clock_reason is not None or confirmed_at is None:
        return _result(
            PrivateTtyReviewDecision.STOP,
            clock_reason or PrivateTtyReviewReason.CLOCK_INVALID,
        )
    reason = _revalidate_supplied_preview(
        preview,
        packet,
        scope,
        candidates,
        display,
        now=confirmed_at,
    )
    if reason is not None:
        return _result(PrivateTtyReviewDecision.STOP, reason)

    receipt = PrivateTtyReviewReceipt(
        packet_revision_sha256=preview.packet_revision_sha256,
        preview_sha256=preview.preview_sha256,
        account_id=preview.account_id,
        action=preview.action,
        confirmed_at=confirmed_at,
    )
    return PrivateTtyReviewResult(
        PrivateTtyReviewDecision.CONFIRMED_FOR_REVIEW_ONLY,
        (),
        receipt,
    )


def _revalidate_supplied_preview(
    preview: PrivateReviewPreview,
    packet: ReviewContentPacket,
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    display: ReviewDisplaySnapshot,
    *,
    now: datetime,
) -> PrivateTtyReviewReason | None:
    if not _is_utc(now):
        return PrivateTtyReviewReason.CLOCK_INVALID
    if not _is_utc(preview.expires_at):
        return PrivateTtyReviewReason.PREVIEW_MISMATCH
    if now >= preview.expires_at:
        return PrivateTtyReviewReason.EXPIRED
    try:
        result = build_private_review_preview(packet, scope, candidates, display, now=now)
    except Exception:  # noqa: BLE001 - validation must fail closed without exposing DTO data
        return PrivateTtyReviewReason.PREVIEW_MISMATCH
    if (
        type(result) is not PrivateReviewPreviewResult
        or result.decision is not PrivateReviewDecision.READY_FOR_PRIVATE_TTY
        or result.reason_codes != ()
        or type(result.preview) is not PrivateReviewPreview
        or result.preview != preview
        or result.preview.packet_revision_sha256 != preview.packet_revision_sha256
        or result.preview.preview_sha256 != preview.preview_sha256
        or result.preview.display_context_sha256 != preview.display_context_sha256
    ):
        return PrivateTtyReviewReason.PREVIEW_MISMATCH
    if not _preview_visible_fields_are_safe(preview):
        return PrivateTtyReviewReason.INVALID_VISIBLE_TEXT
    return None


def _preview_visible_fields_are_safe(preview: PrivateReviewPreview) -> bool:
    if (
        type(preview.account_id) is not UUID
        or type(preview.action) is not ActionKind
        or type(preview.source) is not CandidateSource
        or type(preview.provenance) is not ObservationProvenance
        or type(preview.content_origin) is not ContentOrigin
        or type(preview.rights_evidence) is not RightsEvidence
        or type(preview.rights_evidence.rights_basis) is not RightsBasis
        or type(preview.rights_evidence.job_facts_declaration) is not JobFactsDeclaration
        or type(preview.rights_evidence.right_to_use_declared) is not bool
        or preview.rights_evidence.right_to_use_declared is not True
        or type(preview.rights_evidence.source_attribution) is not str
        or not preview.rights_evidence.source_attribution.strip()
        or preview.rights_evidence.rights_basis is RightsBasis.UNKNOWN
        or preview.rights_evidence.job_facts_declaration is JobFactsDeclaration.UNKNOWN
        or preview.content_origin is ContentOrigin.UNKNOWN
        or not _is_sha256(preview.packet_revision_sha256)
        or not _is_sha256(preview.preview_sha256)
        or not _is_sha256(preview.display_context_sha256)
        or not _is_sha256(preview.target_sha256)
        or not _is_sha256(preview.context_sha256)
        or not _is_sha256(preview.context_evidence_sha256)
        or not _is_sha256(preview.policy_sha256)
        or not _optional_sha256(preview.root_sha256)
        or not _optional_sha256(preview.parent_sha256)
        or not _optional_sha256(preview.reply_sha256)
        or not _is_utc(preview.packet_expires_at)
        or not _is_utc(preview.content_expires_at)
        or not _is_utc(preview.expires_at)
    ):
        return False

    text_values: tuple[str | None, ...] = (
        preview.actor_id,
        preview.candidate_fingerprint,
        preview.thread_id,
        preview.reply_id,
        preview.root_id,
        preview.parent_id,
        preview.reply_text,
        preview.root_text,
        preview.parent_text,
        preview.final_content_text,
        preview.normalization_version,
        preview.rights_evidence.source_attribution,
        preview.rights_evidence.job_facts_sha256,
    )
    if any(value is not None and not _safe_visible_text(value) for value in text_values):
        return False
    if (
        not _valid_remote_id(preview.actor_id)
        or not _is_sha256(preview.candidate_fingerprint)
        or not _is_sha256(preview.rights_evidence.job_facts_sha256)
        or preview.normalization_version != "nfc-lf-v1"
        or not preview.final_content_text
        or not preview.final_content_text.strip()
        or not unicodedata.is_normalized("NFC", preview.final_content_text)
    ):
        return False

    if preview.action is ActionKind.POST_TEXT:
        return (
            preview.source is CandidateSource.OWN_ACCOUNT
            and preview.provenance
            in {
                ObservationProvenance.LOCAL_OPERATOR_PACKET,
                ObservationProvenance.SYNTHETIC_FIXTURE,
            }
            and _rights_origin_pair_is_valid(preview)
            and all(
                value is None
                for value in (
                    preview.thread_id,
                    preview.reply_id,
                    preview.root_id,
                    preview.parent_id,
                    preview.reply_text,
                    preview.root_text,
                    preview.parent_text,
                )
            )
        )
    if preview.action is ActionKind.REPLY:
        return (
            preview.source is CandidateSource.INBOUND_REPLY
            and preview.provenance
            in {
                ObservationProvenance.OFFICIAL_API,
                ObservationProvenance.SYNTHETIC_FIXTURE,
            }
            and _rights_origin_pair_is_valid(preview)
            and _valid_remote_id(preview.actor_id)
            and _valid_remote_id(preview.thread_id)
            and _valid_remote_id(preview.reply_id)
            and _valid_remote_id(preview.root_id)
            and _valid_remote_id(preview.parent_id)
            and preview.thread_id == preview.root_id
            and all(
                type(value) is str and bool(value.strip())
                for value in (preview.reply_text, preview.root_text, preview.parent_text)
            )
        )
    return False


def _rights_origin_pair_is_valid(preview: PrivateReviewPreview) -> bool:
    rights = preview.rights_evidence
    if preview.content_origin is ContentOrigin.SYNTHETIC_FIXTURE:
        return (
            preview.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
            and rights.rights_basis is RightsBasis.SYNTHETIC_FIXTURE
            and rights.job_facts_declaration is JobFactsDeclaration.SYNTHETIC_FIXTURE
        )
    if preview.content_origin is ContentOrigin.OPERATOR_AUTHORED:
        return (
            preview.provenance is not ObservationProvenance.SYNTHETIC_FIXTURE
            and rights.rights_basis is not RightsBasis.SYNTHETIC_FIXTURE
            and rights.job_facts_declaration is JobFactsDeclaration.OPERATOR_VERIFIED
        )
    return False


def _render_preview(preview: PrivateReviewPreview) -> str | None:
    """Return the complete bounded human view with every value quoted."""

    if not _preview_visible_fields_are_safe(preview):
        return None
    rights = preview.rights_evidence
    action = "OWN_POST" if preview.action is ActionKind.POST_TEXT else "EXACT_TARGET_REPLY"
    lines = [
        "LOCAL PRIVATE REVIEW / NO DISPATCH\n",
        f"Account UUID: {_quoted(str(preview.account_id))}\n",
        f"API actor identity: {_quoted(preview.actor_id)}\n",
        f"Action: {action}\n",
        f"Source: {preview.source.value}\n",
        f"Observation provenance: {preview.provenance.value}\n",
    ]
    if preview.action is ActionKind.POST_TEXT:
        lines.extend(
            (
                "Target: the selected account's own post\n",
                "Thread ID: NONE\n",
                "Root ID: NONE\n",
                "Parent ID: NONE\n",
                "Reply ID: NONE\n",
            )
        )
    else:
        lines.extend(
            (
                "Exact reply target and visible context:\n",
                f"Thread ID: {_quoted(_required_text(preview.thread_id))}\n",
                f"Root ID: {_quoted(_required_text(preview.root_id))}\n",
                f"Root text (complete): {_quoted(_required_text(preview.root_text))}\n",
                f"Parent ID: {_quoted(_required_text(preview.parent_id))}\n",
                f"Parent text (complete): {_quoted(_required_text(preview.parent_text))}\n",
                f"Reply ID: {_quoted(_required_text(preview.reply_id))}\n",
                f"Reply text (complete): {_quoted(_required_text(preview.reply_text))}\n",
            )
        )
    lines.extend(
        (
            (
                f"Final normalized operator content ({preview.normalization_version}): "
                f"{_quoted(preview.final_content_text)}\n"
            ),
            f"Content origin: {preview.content_origin.value}\n",
            "Rights/use evidence:\n",
            f"Rights basis: {rights.rights_basis.value}\n",
            f"Right to use declared: {'YES' if rights.right_to_use_declared else 'NO'}\n",
            (
                "Source attribution: "
                f"{_quoted(rights.source_attribution) if rights.source_attribution is not None else 'NONE'}\n"
            ),
            f"Job facts declaration: {rights.job_facts_declaration.value}\n",
            f"Job-facts digest label: {_quoted(_required_text(rights.job_facts_sha256))}\n",
            f"Packet expires at UTC: {preview.packet_expires_at.isoformat()}\n",
            f"Content expires at UTC: {preview.content_expires_at.isoformat()}\n",
            f"Review expires at UTC: {preview.expires_at.isoformat()}\n",
            f"packet_revision_sha256: {preview.packet_revision_sha256}\n",
            f"preview_sha256: {preview.preview_sha256}\n",
            "No approval is persisted and no API call occurs.\n",
        )
    )
    try:
        rendered = "".join(lines)
        rendered.encode("utf-8", errors="strict")
    except UnicodeError, TypeError, ValueError:
        return None
    return rendered


def _confirmation_challenge(preview: PrivateReviewPreview) -> str | None:
    if not _is_sha256(preview.preview_sha256) or not _is_sha256(preview.packet_revision_sha256):
        return None
    return f"CONFIRM {preview.preview_sha256[:16]} {preview.packet_revision_sha256[:16]}"


def _exact_line(entered: str, challenge: str) -> bool:
    if not entered.isascii():
        return False
    if entered.endswith("\r\n"):
        return entered[:-2] == challenge
    if entered.endswith("\n"):
        return entered[:-1] == challenge
    return False


def _environment_snapshot(environment: object) -> dict[str, str] | None:
    if not isinstance(environment, Mapping):
        return None
    try:
        typed_environment = cast(Mapping[str, str], environment)
        entries = tuple(typed_environment.items())
    except Exception:  # noqa: BLE001 - injected mapping may be malformed
        return None
    if any(type(key) is not str or type(value) is not str for key, value in entries):
        return None
    return dict(entries)


def _valid_stream_set(stdin: object, stdout: object, stderr: object) -> bool:
    if stdin is None or stdout is None or stderr is None:
        return False
    if stdin is stdout or stdin is stderr or stdout is stderr:
        return False
    try:
        return all(
            callable(getattr(stream, method, None))
            for stream, methods in (
                (stdin, ("isatty", "readline")),
                (stdout, ("isatty",)),
                (stderr, ("isatty", "write", "flush")),
            )
            for method in methods
        )
    except Exception:  # noqa: BLE001 - malformed injected streams fail closed
        return False


def _all_streams_are_tty(stdin: TextIO, stdout: TextIO, stderr: TextIO) -> bool:
    try:
        return all(stream.isatty() is True for stream in (stdin, stdout, stderr))
    except Exception:  # noqa: BLE001 - stream errors cannot fall back to public output
        return False


def _read_clock(
    now_utc: Callable[[], datetime],
) -> tuple[datetime | None, PrivateTtyReviewReason | None]:
    try:
        value = now_utc()
    except Exception:  # noqa: BLE001 - injected clock failure must fail closed
        return None, PrivateTtyReviewReason.CLOCK_INVALID
    if not _is_utc(value):
        return None, PrivateTtyReviewReason.CLOCK_INVALID
    return value, None


def _safe_visible_text(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return not any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} and character != "\n"
        for character in value
    )


def _write_complete(stream: TextIO, value: str) -> None:
    written = stream.write(value)
    if type(written) is not int or written != len(value):
        raise OSError("incomplete terminal write")
    stream.flush()


def _displayed_line_count(value: str) -> int:
    return value.count("\n") + (0 if value.endswith("\n") else 1)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _required_text(value: str | None) -> str:
    if type(value) is not str:
        raise ValueError("invalid visible field")
    return value


def _optional_sha256(value: object) -> bool:
    return value is None or _is_sha256(value)


def _valid_remote_id(value: object) -> bool:
    return type(value) is str and _REMOTE_ID.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _is_utc(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() == timedelta(0)
    except Exception:  # noqa: BLE001 - malformed tzinfo is invalid input
        return False


def _result(
    decision: PrivateTtyReviewDecision,
    reason: PrivateTtyReviewReason,
    receipt: PrivateTtyReviewReceipt | None = None,
) -> PrivateTtyReviewResult:
    return PrivateTtyReviewResult(decision, (reason,), receipt)
