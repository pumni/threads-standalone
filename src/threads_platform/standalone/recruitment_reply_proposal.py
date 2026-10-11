"""Ephemeral synthetic reply proposals for human review only."""

from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Protocol

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


def _validate_reply(value: object) -> None:
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
        raise ReplyProposalError("INVALID_PROPOSAL_OUTPUT")
