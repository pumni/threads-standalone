"""Ephemeral synthetic reply proposals for human review only."""

from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from threads_platform.standalone.nurture_discovery import NurtureDiscoverySource
from threads_platform.standalone.nurture_runner import (
    NurtureShortlistCandidate,
    NurtureShortlistResult,
)

_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_MAX_SHORTLIST_CANDIDATES = 10
_MAX_REPLY_CHARS = 500
_MAX_GENERATION_TIMEOUT_SECONDS = 30.0


class ReplyProposalError(ValueError):
    """Safe proposal failure containing only a stable error code."""

    code: str

    def __init__(self, code: str) -> None:
        if code not in {
            "INVALID_SHORTLIST",
            "INVALID_SELECTION",
            "CANDIDATE_NOT_ELIGIBLE",
            "CANDIDATE_CONTEXT_UNAVAILABLE",
            "PROPOSAL_TIMEOUT",
            "GENERATOR_FAILED",
            "INVALID_PROPOSAL_OUTPUT",
            "TARGET_BINDING_MISMATCH",
            "INVALID_EDITOR_SESSION",
            "EDITOR_SESSION_CLOSED",
            "INVALID_EDITED_REPLY",
        }:
            raise ValueError("invalid reply proposal error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ReplyProposalInput:
    """Bounded candidate context. Its text is untrusted and never appears in repr."""

    source: NurtureDiscoverySource | Literal["INBOUND_REPLY"]
    score: int | None
    reason_codes: tuple[str, ...]
    candidate_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.source not in {*NurtureDiscoverySource, "INBOUND_REPLY"}
            or (self.score is not None and (type(self.score) is not int or self.score < 0))
            or type(self.reason_codes) is not tuple
            or len(self.reason_codes) > 16
            or any(
                type(code) is not str or _REASON_CODE.fullmatch(code) is None
                for code in self.reason_codes
            )
            or type(self.candidate_text) is not str
            or not self.candidate_text.strip()
            or len(self.candidate_text) > 500
        ):
            raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")

    def __repr__(self) -> str:
        return (
            "ReplyProposalInput("
            f"source={self.source}, score={self.score!r}, "
            f"reason_codes={self.reason_codes!r}, candidate_text=<redacted>)"
        )


class ReplyProposalGenerator(Protocol):
    """Injectable async generator boundary; production code provides no live client."""

    async def generate_reply(self, request: ReplyProposalInput) -> str: ...


class DeterministicFakeReplyGenerator:
    """Offline generator used by the demo and synthetic tests only."""

    async def generate_reply(self, _request: ReplyProposalInput) -> str:
        return (
            "Thanks for sharing this opportunity. What skills or experience would help "
            "someone thrive in this role?"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticReplyProposal:
    """An ephemeral proposal bound to the exact in-memory shortlist candidate."""

    _bound_candidate: NurtureShortlistCandidate = field(repr=False)
    suggested_reply: str = field(repr=False)
    source: Literal["FAKE_SYNTHETIC"] = "FAKE_SYNTHETIC"
    status: Literal["PROPOSED_FOR_HUMAN_REVIEW"] = "PROPOSED_FOR_HUMAN_REVIEW"
    approval_status: Literal["NOT_APPROVED"] = "NOT_APPROVED"
    delivery_status: Literal["NOT_SENT"] = "NOT_SENT"

    def __post_init__(self) -> None:
        if (
            type(self._bound_candidate) is not NurtureShortlistCandidate
            or type(self._bound_candidate.fingerprint) is not str
            or _FINGERPRINT.fullmatch(self._bound_candidate.fingerprint) is None
            or self._bound_candidate.status != "ELIGIBLE"
            or self._bound_candidate.action_hint != "REVIEW_CANDIDATE"
            or self._bound_candidate.text is None
            or not self._bound_candidate.text.strip()
            or self._bound_candidate.text_truncated
            or type(self.source) is not str
            or self.source != "FAKE_SYNTHETIC"
            or type(self.status) is not str
            or self.status != "PROPOSED_FOR_HUMAN_REVIEW"
            or type(self.approval_status) is not str
            or self.approval_status != "NOT_APPROVED"
            or type(self.delivery_status) is not str
            or self.delivery_status != "NOT_SENT"
        ):
            raise ReplyProposalError("TARGET_BINDING_MISMATCH")
        _validate_reply(self.suggested_reply)

    @property
    def target_fingerprint(self) -> str:
        return self._bound_candidate.fingerprint

    @property
    def candidate_source(self) -> NurtureDiscoverySource | Literal["INBOUND_REPLY"]:
        return self._bound_candidate.source

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return self._bound_candidate.reason_codes

    def is_bound_to(self, candidate: NurtureShortlistCandidate) -> bool:
        """Return whether this proposal retains the exact selected candidate object."""

        return self._bound_candidate is candidate

    def __repr__(self) -> str:
        return (
            "SyntheticReplyProposal("
            "target_fingerprint=<redacted>, source=FAKE_SYNTHETIC, "
            "status=PROPOSED_FOR_HUMAN_REVIEW, approval_status=NOT_APPROVED, "
            "delivery_status=NOT_SENT, suggested_reply=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HumanReplyPreview:
    """Preview carrying the same candidate object that was used for generation."""

    candidate: NurtureShortlistCandidate = field(repr=False)
    proposal: SyntheticReplyProposal = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not NurtureShortlistCandidate
            or type(self.proposal) is not SyntheticReplyProposal
            or not self.proposal.is_bound_to(self.candidate)
            or self.proposal.target_fingerprint != self.candidate.fingerprint
        ):
            raise ReplyProposalError("TARGET_BINDING_MISMATCH")

    def __repr__(self) -> str:
        return "HumanReplyPreview(candidate=<bound>, proposal=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class HumanReplyEditorialSession:
    """Ephemeral edits tied to the exact proposal target; never an approval."""

    preview: HumanReplyPreview = field(repr=False)
    target_fingerprint: str = field(repr=False)
    current_reply: str | None = field(repr=False)
    editorial_disposition: Literal["EDITING", "KEPT_FOR_SESSION", "DISCARDED"] = "EDITING"

    def __post_init__(self) -> None:
        if (
            type(self.preview) is not HumanReplyPreview
            or self.preview.candidate.status != "ELIGIBLE"
            or self.preview.candidate.action_hint != "REVIEW_CANDIDATE"
            or type(self.target_fingerprint) is not str
            or self.target_fingerprint != self.preview.candidate.fingerprint
            or self.target_fingerprint != self.preview.proposal.target_fingerprint
            or not self.preview.proposal.is_bound_to(self.preview.candidate)
            or type(self.editorial_disposition) is not str
            or self.editorial_disposition not in {"EDITING", "KEPT_FOR_SESSION", "DISCARDED"}
        ):
            raise ReplyProposalError("INVALID_EDITOR_SESSION")
        if self.editorial_disposition == "DISCARDED":
            if self.current_reply is not None:
                raise ReplyProposalError("INVALID_EDITOR_SESSION")
        else:
            _validate_reply(self.current_reply, error_code="INVALID_EDITED_REPLY")

    @classmethod
    def begin(cls, preview: HumanReplyPreview) -> HumanReplyEditorialSession:
        if type(preview) is not HumanReplyPreview:
            raise ReplyProposalError("INVALID_EDITOR_SESSION")
        return cls(preview, preview.proposal.target_fingerprint, preview.proposal.suggested_reply)

    @property
    def candidate(self) -> NurtureShortlistCandidate:
        return self.preview.candidate

    @property
    def approval_status(self) -> Literal["NOT_APPROVED_FOR_SENDING"]:
        return "NOT_APPROVED_FOR_SENDING"

    @property
    def delivery_status(self) -> Literal["NOT_SENT"]:
        return "NOT_SENT"

    def is_bound_to(self, candidate: NurtureShortlistCandidate) -> bool:
        return (
            type(candidate) is NurtureShortlistCandidate
            and self.preview.candidate is candidate
            and self.target_fingerprint == candidate.fingerprint
            and self.preview.proposal.is_bound_to(candidate)
        )

    def edit(self, value: object) -> HumanReplyEditorialSession:
        self._require_editing()
        _validate_reply(value, error_code="INVALID_EDITED_REPLY")
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            cast(str, value),
            "EDITING",
        )

    def keep_for_session(self) -> HumanReplyEditorialSession:
        self._require_editing()
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            self.current_reply,
            "KEPT_FOR_SESSION",
        )

    def discard(self) -> HumanReplyEditorialSession:
        self._require_editing()
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            None,
            "DISCARDED",
        )

    def _require_editing(self) -> None:
        if self.editorial_disposition != "EDITING":
            raise ReplyProposalError("EDITOR_SESSION_CLOSED")

    def __repr__(self) -> str:
        return (
            "HumanReplyEditorialSession("
            "candidate=<bound>, target_fingerprint=<redacted>, "
            f"editorial_disposition={self.editorial_disposition}, current_reply=<redacted>, "
            "approval_status=NOT_APPROVED_FOR_SENDING, delivery_status=NOT_SENT)"
        )


async def create_human_reply_preview(
    shortlist: NurtureShortlistResult,
    selection: int,
    generator: ReplyProposalGenerator,
    *,
    timeout_seconds: float = 5.0,
) -> HumanReplyPreview:
    """Generate one bounded proposal for a selected eligible candidate, without writes."""

    if (
        type(shortlist) is not NurtureShortlistResult
        or type(shortlist.candidates) is not tuple
        or not 1 <= len(shortlist.candidates) <= _MAX_SHORTLIST_CANDIDATES
        or any(type(item) is not NurtureShortlistCandidate for item in shortlist.candidates)
    ):
        raise ReplyProposalError("INVALID_SHORTLIST")
    if type(selection) is not int or not 1 <= selection <= len(shortlist.candidates):
        raise ReplyProposalError("INVALID_SELECTION")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_GENERATION_TIMEOUT_SECONDS
    ):
        raise ReplyProposalError("GENERATOR_FAILED")

    candidate = shortlist.candidates[selection - 1]
    if candidate.status != "ELIGIBLE" or candidate.action_hint != "REVIEW_CANDIDATE":
        raise ReplyProposalError("CANDIDATE_NOT_ELIGIBLE")
    if candidate.text is None or not candidate.text.strip() or candidate.text_truncated:
        raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")

    request = ReplyProposalInput(
        source=candidate.source,
        score=candidate.score,
        reason_codes=candidate.reason_codes,
        candidate_text=candidate.text,
    )
    try:
        generated = await asyncio.wait_for(
            generator.generate_reply(request),
            timeout=float(timeout_seconds),
        )
    except TimeoutError:
        raise ReplyProposalError("PROPOSAL_TIMEOUT") from None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — exception details may contain private prompt data.
        raise ReplyProposalError("GENERATOR_FAILED") from None

    _validate_reply(generated)
    proposal = SyntheticReplyProposal(candidate, generated)
    return HumanReplyPreview(candidate, proposal)


def _validate_reply(
    value: object,
    *,
    error_code: Literal["INVALID_PROPOSAL_OUTPUT", "INVALID_EDITED_REPLY"] = (
        "INVALID_PROPOSAL_OUTPUT"
    ),
) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_REPLY_CHARS
        or not value.strip()
        or value != value.strip()
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in value
        )
    ):
        raise ReplyProposalError(error_code)
