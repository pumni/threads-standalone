"""Bounded, in-memory synthetic Browser Jobs observation contract.

This module defines a fake session seam only. It has no browser, network, or
persistence implementation and cannot produce browser-verified provenance.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.standalone.recruitment_job_vacancy import (
    AuthorEvidenceStatus,
    CompletenessBasis,
    ObservationSource,
    PostKind,
    PostObservation,
    TextCompleteness,
    TextProvenance,
)

SYNTHETIC_ORIGIN = "https://threads.example.invalid"
SYNTHETIC_SEARCH_URL = f"{SYNTHETIC_ORIGIN}/search"

MAX_QUERY_CODEPOINTS = 120
MAX_BATCHES = 5
MAX_CARDS_PER_BATCH = 20
MAX_EXAMINED_CANDIDATES = 100
MAX_DETAIL_READS = 20
MAX_TEXT_CODEPOINTS = 10_000
MAX_DURATION_SECONDS = 30.0
MAX_TARGET_URL_LENGTH = 2_048
MAX_LOCAL_ID_LENGTH = 128

_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class FakePageState(StrEnum):
    READY = "READY"
    REDIRECTED = "REDIRECTED"
    CHALLENGE = "CHALLENGE"
    LOGIN_WALL = "LOGIN_WALL"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


class FakeBodyVisibility(StrEnum):
    FULL_VISIBLE = "FULL_VISIBLE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ObservedPostKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    REPOST = "REPOST"
    QUOTE = "QUOTE"
    REPLY = "REPLY"
    UNKNOWN = "UNKNOWN"


class PostObservationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ObservationRunStatus(StrEnum):
    COMPLETE = "COMPLETE"
    CAP_REACHED = "CAP_REACHED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class ObservationReasonCode(StrEnum):
    INVALID_ACCOUNT = "INVALID_ACCOUNT"
    INVALID_QUERY = "INVALID_QUERY"
    INVALID_TARGET = "INVALID_TARGET"
    INVALID_LIMITS = "INVALID_LIMITS"
    INVALID_CAPTURE_ID = "INVALID_CAPTURE_ID"
    INVALID_CAPTURE_TIMESTAMP = "INVALID_CAPTURE_TIMESTAMP"
    INVALID_SESSION_STATE = "INVALID_SESSION_STATE"
    SESSION_FAILURE = "SESSION_FAILURE"
    CLOCK_FAILURE = "CLOCK_FAILURE"
    CLOCK_MOVED_BACKWARD = "CLOCK_MOVED_BACKWARD"
    STOP_SIGNAL_FAILURE = "STOP_SIGNAL_FAILURE"
    STOP_REQUESTED = "STOP_REQUESTED"
    TIMEOUT = "TIMEOUT"
    WRONG_ORIGIN = "WRONG_ORIGIN"
    REDIRECT_DETECTED = "REDIRECT_DETECTED"
    CHALLENGE = "CHALLENGE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UNCLASSIFIED_STATE = "UNCLASSIFIED_STATE"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    QUERY_MISMATCH = "QUERY_MISMATCH"
    BATCH_MISMATCH = "BATCH_MISMATCH"
    CARD_ID_INVALID = "CARD_ID_INVALID"
    CARD_ID_REUSED = "CARD_ID_REUSED"
    AUTHOR_MISMATCH = "AUTHOR_MISMATCH"
    BATCH_LIMIT_REACHED = "BATCH_LIMIT_REACHED"
    TRANCHE_LIMIT_REACHED = "TRANCHE_LIMIT_REACHED"
    CANDIDATE_LIMIT_REACHED = "CANDIDATE_LIMIT_REACHED"
    DETAIL_LIMIT_REACHED = "DETAIL_LIMIT_REACHED"
    TARGET_NOT_CAPTURED = "TARGET_NOT_CAPTURED"
    POST_MISSING = "POST_MISSING"
    TARGET_MOVED = "TARGET_MOVED"
    TEXT_LIMIT_REACHED = "TEXT_LIMIT_REACHED"
    TEXT_PARTIAL = "TEXT_PARTIAL"
    TEXT_COMPLETENESS_UNKNOWN = "TEXT_COMPLETENESS_UNKNOWN"
    TEXT_UNAVAILABLE = "TEXT_UNAVAILABLE"
    IMAGE_ONLY = "IMAGE_ONLY"
    AUTHOR_UNAVAILABLE = "AUTHOR_UNAVAILABLE"
    POST_KIND_UNKNOWN = "POST_KIND_UNKNOWN"
    RELATIONSHIP_UNAVAILABLE = "RELATIONSHIP_UNAVAILABLE"
    TIMESTAMP_UNAVAILABLE = "TIMESTAMP_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ObservationLimits:
    """Per-run bounds which may be reduced but never raised above the contract."""

    max_query_codepoints: int = MAX_QUERY_CODEPOINTS
    max_batches: int = MAX_BATCHES
    max_cards_per_batch: int = MAX_CARDS_PER_BATCH
    max_examined_candidates: int = MAX_EXAMINED_CANDIDATES
    max_detail_reads: int = MAX_DETAIL_READS
    max_text_codepoints: int = MAX_TEXT_CODEPOINTS
    max_duration_seconds: float = MAX_DURATION_SECONDS

    def __post_init__(self) -> None:
        integer_limits = (
            (self.max_query_codepoints, MAX_QUERY_CODEPOINTS),
            (self.max_batches, MAX_BATCHES),
            (self.max_cards_per_batch, MAX_CARDS_PER_BATCH),
            (self.max_examined_candidates, MAX_EXAMINED_CANDIDATES),
            (self.max_detail_reads, MAX_DETAIL_READS),
            (self.max_text_codepoints, MAX_TEXT_CODEPOINTS),
        )
        if any(
            type(value) is not int or value < 1 or value > maximum
            for value, maximum in integer_limits
        ):
            raise ValueError("INVALID_LIMITS")
        if (
            isinstance(self.max_duration_seconds, bool)
            or not isfinite(self.max_duration_seconds)
            or not 0 < self.max_duration_seconds <= MAX_DURATION_SECONDS
        ):
            raise ValueError("INVALID_LIMITS")


DEFAULT_OBSERVATION_LIMITS = ObservationLimits()


@dataclass(frozen=True, slots=True, repr=False)
class FakeJobCardV1:
    """Synthetic feed card; values are held only in memory and repr-redacted."""

    account_uuid: UUID = field(repr=False)
    local_card_id: str = field(repr=False)
    canonical_post_url: str = field(repr=False)
    author_marker: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "FakeJobCardV1(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class FakeJobFeedBatchV1:
    """One visible synthetic tranche after the fake page has rendered."""

    batch_index: int
    account_uuid: UUID = field(repr=False)
    query: str = field(repr=False)
    page_url: str = field(repr=False)
    state: FakePageState
    cards: tuple[FakeJobCardV1, ...] = field(repr=False)
    has_more: bool

    def __repr__(self) -> str:
        return (
            "FakeJobFeedBatchV1("
            f"batch_index={self.batch_index}, state={self.state!s}, "
            f"card_count={len(self.cards)}, has_more={self.has_more})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FakeJobPostDetailV1:
    """Synthetic visible detail data returned for one exact requested target."""

    requested_url: str = field(repr=False)
    page_url: str = field(repr=False)
    canonical_post_url: str = field(repr=False)
    account_uuid: UUID = field(repr=False)
    state: FakePageState
    author_marker: str | None = field(repr=False)
    body: str | None = field(repr=False)
    body_visibility: FakeBodyVisibility
    post_kind: ObservedPostKind = ObservedPostKind.ORIGINAL
    body_truncated: bool = False
    collapsed_content: bool = False
    image_only: bool = False
    published_at_utc: datetime | None = field(default=None, repr=False)
    quoted_post_url: str | None = field(default=None, repr=False)
    quoted_author_label: str | None = field(default=None, repr=False)
    parent_post_url: str | None = field(default=None, repr=False)
    parent_author_label: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "FakeJobPostDetailV1("
            f"state={self.state!s}, body_visibility={self.body_visibility!s}, "
            f"body_codepoints={len(self.body) if self.body is not None else 0})"
        )


class FakeJobBrowserSession(Protocol):
    """In-memory fake page seam; this module provides no live implementation."""

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> FakeJobFeedBatchV1: ...

    def scroll_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        next_batch_index: int,
        increment: int,
        max_cards: int,
    ) -> FakeJobFeedBatchV1: ...

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> FakeJobPostDetailV1 | None: ...


class FakeClock(Protocol):
    """Injected monotonic clock used to enforce a deterministic fake deadline."""

    def monotonic(self) -> float: ...


class FakeStopSignal(Protocol):
    """Injected foreground stop/cancel signal."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class CapturedJobTargetV1:
    """Target identity captured from a visible card before a scroll call."""

    account_uuid: UUID = field(repr=False)
    canonical_post_url: str = field(repr=False)
    local_card_id: str = field(repr=False)
    author_label: str | None = field(default=None, repr=False)
    author_marker: Literal["UNVERIFIED"] = "UNVERIFIED"

    def __repr__(self) -> str:
        return "CapturedJobTargetV1(target=<redacted>, card_id=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserJobObservationV1:
    """One bounded synthetic observation, never evidence of a live browser read."""

    schema_version: Literal[1]
    observation_id: str = field(repr=False)
    account_uuid: UUID = field(repr=False)
    canonical_post_url: str = field(repr=False)
    local_card_id: str = field(repr=False)
    observed_at_utc: datetime = field(repr=False)
    post_revision_id: str | None = field(default=None, repr=False)
    source: Literal["SYNTHETIC_FIXTURE"] = "SYNTHETIC_FIXTURE"
    text_provenance: Literal["SYNTHETIC_FIXTURE_TEXT"] = "SYNTHETIC_FIXTURE_TEXT"
    completeness_basis: Literal["FIXTURE_DECLARED"] = "FIXTURE_DECLARED"
    author_marker: Literal["UNVERIFIED"] = "UNVERIFIED"
    author_label: str | None = field(default=None, repr=False)
    visible_text: str | None = field(default=None, repr=False)
    text_completeness: FakeBodyVisibility = FakeBodyVisibility.UNKNOWN
    text_truncated: bool = False
    status: PostObservationStatus = PostObservationStatus.UNKNOWN
    reason_codes: tuple[ObservationReasonCode, ...] = ()
    post_kind: ObservedPostKind = ObservedPostKind.UNKNOWN
    published_at_utc: datetime | None = field(default=None, repr=False)
    quoted_post_url: str | None = field(default=None, repr=False)
    quoted_author_label: str | None = field(default=None, repr=False)
    parent_post_url: str | None = field(default=None, repr=False)
    parent_author_label: str | None = field(default=None, repr=False)
    related_author_marker: Literal["UNVERIFIED"] | None = None

    def __repr__(self) -> str:
        return (
            "BrowserJobObservationV1("
            f"source={self.source!r}, status={self.status.value!r}, "
            f"text_completeness={self.text_completeness.value!r}, "
            f"text_codepoints={len(self.visible_text) if self.visible_text is not None else 0}, "
            f"reason_codes={tuple(code.value for code in self.reason_codes)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BrowserJobObservationRunV1:
    """One finite fake read with operation caps separate from post quality."""

    status: ObservationRunStatus
    captured_targets: tuple[CapturedJobTargetV1, ...] = field(repr=False)
    observations: tuple[BrowserJobObservationV1, ...] = field(repr=False)
    examined_candidate_count: int
    batch_count: int
    detail_count: int
    reason_codes: tuple[ObservationReasonCode, ...] = ()

    def __repr__(self) -> str:
        return (
            "BrowserJobObservationRunV1("
            f"status={self.status.value!r}, batches={self.batch_count}, "
            f"examined_candidates={self.examined_candidate_count}, "
            f"captured_targets={len(self.captured_targets)}, details={self.detail_count}, "
            f"reason_codes={tuple(code.value for code in self.reason_codes)!r})"
        )


def _new_captured_targets() -> list[CapturedJobTargetV1]:
    return []


def _new_observations() -> list[BrowserJobObservationV1]:
    return []


@dataclass(slots=True)
class _RunState:
    captured: list[CapturedJobTargetV1] = field(default_factory=_new_captured_targets)
    observations: list[BrowserJobObservationV1] = field(default_factory=_new_observations)
    examined_candidates: int = 0
    batch_count: int = 0
    detail_count: int = 0

    def result(
        self,
        status: ObservationRunStatus,
        reason: ObservationReasonCode | None = None,
    ) -> BrowserJobObservationRunV1:
        reasons = () if reason is None else (reason,)
        return BrowserJobObservationRunV1(
            status=status,
            captured_targets=tuple(self.captured),
            observations=tuple(self.observations),
            examined_candidate_count=self.examined_candidates,
            batch_count=self.batch_count,
            detail_count=self.detail_count,
            reason_codes=reasons,
        )


def observe_fake_job_feed(
    session: FakeJobBrowserSession,
    *,
    account_uuid: UUID,
    query: str,
    selected_targets: Sequence[str],
    clock: FakeClock,
    stop_signal: FakeStopSignal,
    capture_id: str,
    observed_at_utc: datetime,
    limits: ObservationLimits = DEFAULT_OBSERVATION_LIMITS,
) -> BrowserJobObservationRunV1:
    """Capture bounded fake feed targets, then open only selected exact targets.

    Cards are copied into the local first-seen list before the next scroll call.
    No URL is navigated by this module; the injected session is an in-memory fake.
    """

    state = _RunState()
    if not isinstance(cast(object, limits), ObservationLimits):
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_LIMITS)
    if not isinstance(cast(object, account_uuid), UUID):
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_ACCOUNT)
    raw_capture_id = cast(object, capture_id)
    if not isinstance(raw_capture_id, str) or _LOCAL_ID_RE.fullmatch(raw_capture_id) is None:
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_CAPTURE_ID)
    capture_timestamp = _normalize_capture_timestamp(cast(object, observed_at_utc))
    if capture_timestamp is None:
        return state.result(
            ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_CAPTURE_TIMESTAMP
        )
    raw_query = cast(object, query)
    if (
        not isinstance(raw_query, str)
        or not raw_query
        or len(raw_query) > limits.max_query_codepoints
    ):
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_QUERY)
    if isinstance(selected_targets, (str, bytes)):
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_TARGET)

    unique_targets: list[str] = []
    for target in selected_targets:
        if not _is_synthetic_post_url(target):
            return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_TARGET)
        if target not in unique_targets:
            unique_targets.append(target)
    if len(unique_targets) > limits.max_detail_reads:
        return state.result(
            ObservationRunStatus.CAP_REACHED, ObservationReasonCode.DETAIL_LIMIT_REACHED
        )

    try:
        started_at = clock.monotonic()
    except Exception:  # noqa: BLE001 - injected fake clock failures become fixed reason codes.
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.CLOCK_FAILURE)
    if not _valid_clock_value(started_at):
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.CLOCK_FAILURE)

    gate = _operation_gate(clock, stop_signal, started_at, limits)
    if gate is not None:
        return state.result(*gate)

    try:
        batch = session.read_initial_batch(
            account_uuid=account_uuid,
            query=query,
            max_cards=limits.max_cards_per_batch,
        )
    except Exception:  # noqa: BLE001 - injected fake session errors are deliberately redacted.
        return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.SESSION_FAILURE)
    state.batch_count = 1

    seen_targets: dict[str, CapturedJobTargetV1] = {}
    target_positions: dict[str, int] = {}
    card_id_targets: dict[str, str] = {}
    expected_batch_index = 0
    while True:
        gate = _operation_gate(clock, stop_signal, started_at, limits)
        if gate is not None:
            return state.result(*gate)
        batch_failure = _validate_feed_batch(
            batch,
            account_uuid=account_uuid,
            query=query,
            expected_batch_index=expected_batch_index,
        )
        if batch_failure is not None:
            return state.result(*batch_failure)

        inspected_in_batch = 0
        for card in batch.cards:
            if inspected_in_batch >= limits.max_cards_per_batch:
                break
            if state.examined_candidates >= limits.max_examined_candidates:
                break
            inspected_in_batch += 1
            state.examined_candidates += 1
            card_failure = _validate_card(card, account_uuid=account_uuid)
            if card_failure is not None:
                return state.result(*card_failure)
            previous_url = card_id_targets.get(card.local_card_id)
            if previous_url is not None and previous_url != card.canonical_post_url:
                return state.result(
                    ObservationRunStatus.STOPPED, ObservationReasonCode.CARD_ID_REUSED
                )
            card_id_targets[card.local_card_id] = card.canonical_post_url

            previous_target = seen_targets.get(card.canonical_post_url)
            if previous_target is not None:
                if (
                    previous_target.author_label is not None
                    and card.author_marker is not None
                    and previous_target.author_label != card.author_marker
                ):
                    return state.result(
                        ObservationRunStatus.STOPPED, ObservationReasonCode.AUTHOR_MISMATCH
                    )
                if previous_target.author_label is None and card.author_marker is not None:
                    updated_target = CapturedJobTargetV1(
                        account_uuid=account_uuid,
                        canonical_post_url=card.canonical_post_url,
                        local_card_id=previous_target.local_card_id,
                        author_label=card.author_marker,
                    )
                    seen_targets[card.canonical_post_url] = updated_target
                    state.captured[target_positions[card.canonical_post_url]] = updated_target
                continue

            captured = CapturedJobTargetV1(
                account_uuid=account_uuid,
                canonical_post_url=card.canonical_post_url,
                local_card_id=card.local_card_id,
                author_label=card.author_marker,
            )
            seen_targets[card.canonical_post_url] = captured
            target_positions[card.canonical_post_url] = len(state.captured)
            state.captured.append(captured)

        if len(batch.cards) > inspected_in_batch:
            if state.examined_candidates >= limits.max_examined_candidates:
                return state.result(
                    ObservationRunStatus.CAP_REACHED,
                    ObservationReasonCode.CANDIDATE_LIMIT_REACHED,
                )
            if len(batch.cards) > limits.max_cards_per_batch:
                return state.result(
                    ObservationRunStatus.CAP_REACHED,
                    ObservationReasonCode.TRANCHE_LIMIT_REACHED,
                )
        if batch.has_more:
            if state.examined_candidates >= limits.max_examined_candidates:
                return state.result(
                    ObservationRunStatus.CAP_REACHED,
                    ObservationReasonCode.CANDIDATE_LIMIT_REACHED,
                )
            if state.batch_count >= limits.max_batches:
                return state.result(
                    ObservationRunStatus.CAP_REACHED, ObservationReasonCode.BATCH_LIMIT_REACHED
                )
            gate = _operation_gate(clock, stop_signal, started_at, limits)
            if gate is not None:
                return state.result(*gate)
            expected_batch_index += 1
            try:
                batch = session.scroll_batch(
                    account_uuid=account_uuid,
                    query=query,
                    next_batch_index=expected_batch_index,
                    increment=1,
                    max_cards=limits.max_cards_per_batch,
                )
            except Exception:  # noqa: BLE001 - injected fake session errors are deliberately redacted.
                return state.result(
                    ObservationRunStatus.FAILED, ObservationReasonCode.SESSION_FAILURE
                )
            state.batch_count += 1
            continue
        break

    for target in unique_targets:
        if target not in seen_targets:
            return state.result(
                ObservationRunStatus.STOPPED, ObservationReasonCode.TARGET_NOT_CAPTURED
            )

    for detail_index, target in enumerate(unique_targets, start=1):
        gate = _operation_gate(clock, stop_signal, started_at, limits)
        if gate is not None:
            return state.result(*gate)
        expected_target = seen_targets[target]
        try:
            detail = session.open_exact_post(account_uuid=account_uuid, canonical_post_url=target)
        except Exception:  # noqa: BLE001 - injected fake session errors are deliberately redacted.
            return state.result(ObservationRunStatus.FAILED, ObservationReasonCode.SESSION_FAILURE)
        state.detail_count += 1
        gate = _operation_gate(clock, stop_signal, started_at, limits)
        if gate is not None:
            return state.result(*gate)
        detail_failure = _validate_detail(
            detail,
            account_uuid=account_uuid,
            target=target,
            expected_author=expected_target.author_label,
        )
        if detail_failure is not None:
            return state.result(*detail_failure)
        assert isinstance(detail, FakeJobPostDetailV1)
        state.observations.append(
            _build_observation(
                detail,
                captured=expected_target,
                detail_index=detail_index,
                capture_id=raw_capture_id,
                observed_at_utc=capture_timestamp,
                max_text_codepoints=limits.max_text_codepoints,
            )
        )

    return state.result(ObservationRunStatus.COMPLETE)


def to_parser_observation(observation: BrowserJobObservationV1) -> PostObservation:
    """Map only synthetic DTOs to BJ-01's pure parser input contract."""

    if (
        observation.source != "SYNTHETIC_FIXTURE"
        or observation.text_provenance != "SYNTHETIC_FIXTURE_TEXT"
        or observation.completeness_basis != "FIXTURE_DECLARED"
    ):
        raise ValueError("SYNTHETIC_PROVENANCE_REQUIRED")
    if observation.visible_text is None or observation.post_revision_id is None:
        raise ValueError("TEXT_UNAVAILABLE")

    completeness = {
        FakeBodyVisibility.FULL_VISIBLE: TextCompleteness.COMPLETE,
        FakeBodyVisibility.PARTIAL: TextCompleteness.PARTIAL,
        FakeBodyVisibility.UNKNOWN: TextCompleteness.UNKNOWN,
    }[observation.text_completeness]
    parser_post_kind = {
        ObservedPostKind.ORIGINAL: PostKind.ORIGINAL,
        ObservedPostKind.REPOST: PostKind.REPOST,
        ObservedPostKind.QUOTE: PostKind.QUOTE,
        ObservedPostKind.REPLY: PostKind.REPLY,
        ObservedPostKind.UNKNOWN: PostKind.UNKNOWN,
    }[observation.post_kind]
    return PostObservation(
        observation_id=observation.observation_id,
        account_uuid=str(observation.account_uuid),
        canonical_post_url=observation.canonical_post_url,
        source=ObservationSource.SYNTHETIC_FIXTURE,
        text_provenance=TextProvenance.SYNTHETIC_FIXTURE_TEXT,
        text_completeness=completeness,
        completeness_basis=CompletenessBasis.FIXTURE_DECLARED,
        post_revision_id=observation.post_revision_id,
        observed_text=observation.visible_text,
        text_truncated=observation.text_truncated,
        post_kind=parser_post_kind,
        observed_author=observation.author_label,
        author_status=(
            AuthorEvidenceStatus.UNVERIFIED
            if observation.author_label is not None
            else AuthorEvidenceStatus.UNKNOWN
        ),
    )


def _operation_gate(
    clock: FakeClock,
    stop_signal: FakeStopSignal,
    started_at: float,
    limits: ObservationLimits,
) -> tuple[ObservationRunStatus, ObservationReasonCode] | None:
    try:
        if stop_signal.is_set():
            return ObservationRunStatus.STOPPED, ObservationReasonCode.STOP_REQUESTED
    except Exception:  # noqa: BLE001 - stop-signal errors become a fixed reason code.
        return ObservationRunStatus.FAILED, ObservationReasonCode.STOP_SIGNAL_FAILURE
    try:
        now = clock.monotonic()
    except Exception:  # noqa: BLE001 - injected fake clock failures become fixed reason codes.
        return ObservationRunStatus.FAILED, ObservationReasonCode.CLOCK_FAILURE
    if not _valid_clock_value(now):
        return ObservationRunStatus.FAILED, ObservationReasonCode.CLOCK_FAILURE
    if now < started_at:
        return ObservationRunStatus.FAILED, ObservationReasonCode.CLOCK_MOVED_BACKWARD
    if now - started_at >= limits.max_duration_seconds:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.TIMEOUT
    return None


def _valid_clock_value(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _validate_feed_batch(
    batch: object,
    *,
    account_uuid: UUID,
    query: str,
    expected_batch_index: int,
) -> tuple[ObservationRunStatus, ObservationReasonCode] | None:
    if not isinstance(batch, FakeJobFeedBatchV1):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    page_state = cast(object, batch.state)
    if not isinstance(page_state, FakePageState):
        return ObservationRunStatus.FAILED, ObservationReasonCode.UNCLASSIFIED_STATE
    state_failure = _page_state_failure(page_state)
    if state_failure is not None:
        return state_failure
    if batch.account_uuid != account_uuid:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.ACCOUNT_MISMATCH
    if batch.query != query:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.QUERY_MISMATCH
    if type(batch.batch_index) is not int or batch.batch_index != expected_batch_index:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.BATCH_MISMATCH
    if batch.page_url != SYNTHETIC_SEARCH_URL:
        if _fake_origin(batch.page_url) != SYNTHETIC_ORIGIN:
            return ObservationRunStatus.STOPPED, ObservationReasonCode.WRONG_ORIGIN
        return ObservationRunStatus.STOPPED, ObservationReasonCode.REDIRECT_DETECTED
    cards = cast(object, batch.cards)
    has_more = cast(object, batch.has_more)
    if not isinstance(cards, tuple) or not isinstance(has_more, bool):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    return None


def _page_state_failure(
    state: FakePageState,
) -> tuple[ObservationRunStatus, ObservationReasonCode] | None:
    reason_by_state = {
        FakePageState.READY: None,
        FakePageState.REDIRECTED: (
            ObservationRunStatus.STOPPED,
            ObservationReasonCode.REDIRECT_DETECTED,
        ),
        FakePageState.CHALLENGE: (ObservationRunStatus.STOPPED, ObservationReasonCode.CHALLENGE),
        FakePageState.LOGIN_WALL: (
            ObservationRunStatus.STOPPED,
            ObservationReasonCode.LOGIN_REQUIRED,
        ),
        FakePageState.RATE_LIMITED: (
            ObservationRunStatus.STOPPED,
            ObservationReasonCode.RATE_LIMITED,
        ),
        FakePageState.UNKNOWN: (
            ObservationRunStatus.FAILED,
            ObservationReasonCode.UNCLASSIFIED_STATE,
        ),
    }
    return reason_by_state[state]


def _validate_card(
    card: object,
    *,
    account_uuid: UUID,
) -> tuple[ObservationRunStatus, ObservationReasonCode] | None:
    if not isinstance(card, FakeJobCardV1):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    if card.account_uuid != account_uuid:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.ACCOUNT_MISMATCH
    local_card_id = cast(object, card.local_card_id)
    if not isinstance(local_card_id, str) or not _LOCAL_ID_RE.fullmatch(local_card_id):
        return ObservationRunStatus.FAILED, ObservationReasonCode.CARD_ID_INVALID
    if not _is_synthetic_post_url(card.canonical_post_url):
        return ObservationRunStatus.STOPPED, ObservationReasonCode.WRONG_ORIGIN
    author_marker = cast(object, card.author_marker)
    if author_marker is not None and (
        not isinstance(author_marker, str) or len(author_marker) > MAX_LOCAL_ID_LENGTH
    ):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    return None


def _validate_detail(
    detail: object,
    *,
    account_uuid: UUID,
    target: str,
    expected_author: str | None,
) -> tuple[ObservationRunStatus, ObservationReasonCode] | None:
    if detail is None:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.POST_MISSING
    if not isinstance(detail, FakeJobPostDetailV1):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    page_state = cast(object, detail.state)
    if not isinstance(page_state, FakePageState):
        return ObservationRunStatus.FAILED, ObservationReasonCode.UNCLASSIFIED_STATE
    state_failure = _page_state_failure(page_state)
    if state_failure is not None:
        return state_failure
    if detail.account_uuid != account_uuid:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.ACCOUNT_MISMATCH
    if _fake_origin(detail.page_url) != SYNTHETIC_ORIGIN:
        return ObservationRunStatus.STOPPED, ObservationReasonCode.WRONG_ORIGIN
    if (
        detail.requested_url != target
        or detail.page_url != target
        or detail.canonical_post_url != target
    ):
        return ObservationRunStatus.STOPPED, ObservationReasonCode.TARGET_MOVED
    author_marker = cast(object, detail.author_marker)
    if (
        expected_author is not None
        and author_marker is not None
        and expected_author != author_marker
    ):
        return ObservationRunStatus.STOPPED, ObservationReasonCode.AUTHOR_MISMATCH
    if author_marker is not None and (
        not isinstance(author_marker, str) or len(author_marker) > MAX_LOCAL_ID_LENGTH
    ):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    body_visibility = cast(object, detail.body_visibility)
    if not isinstance(body_visibility, FakeBodyVisibility):
        return ObservationRunStatus.FAILED, ObservationReasonCode.UNCLASSIFIED_STATE
    post_kind = cast(object, detail.post_kind)
    if not isinstance(post_kind, ObservedPostKind):
        return ObservationRunStatus.FAILED, ObservationReasonCode.UNCLASSIFIED_STATE
    body = cast(object, detail.body)
    if body is not None and not isinstance(body, str):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    body_truncated = cast(object, detail.body_truncated)
    collapsed_content = cast(object, detail.collapsed_content)
    if not isinstance(body_truncated, bool) or not isinstance(collapsed_content, bool):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    image_only = cast(object, detail.image_only)
    if not isinstance(image_only, bool):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    if image_only and (
        body not in (None, "") or body_visibility is FakeBodyVisibility.FULL_VISIBLE
    ):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    published_at = cast(object, detail.published_at_utc)
    if published_at is not None and not isinstance(published_at, datetime):
        return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    for author_value in (detail.quoted_author_label, detail.parent_author_label):
        related_author = cast(object, author_value)
        if related_author is not None and (
            not isinstance(related_author, str) or len(related_author) > MAX_LOCAL_ID_LENGTH
        ):
            return ObservationRunStatus.FAILED, ObservationReasonCode.INVALID_SESSION_STATE
    for related_url in (detail.quoted_post_url, detail.parent_post_url):
        if related_url is not None and not _is_synthetic_post_url(related_url):
            return ObservationRunStatus.STOPPED, ObservationReasonCode.WRONG_ORIGIN
    return None


def _normalize_capture_timestamp(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            return None
        return value.astimezone(UTC)
    except Exception:  # noqa: BLE001 - invalid injected timestamps become a fixed reason code.
        return None


def _build_observation(
    detail: FakeJobPostDetailV1,
    *,
    captured: CapturedJobTargetV1,
    detail_index: int,
    capture_id: str,
    observed_at_utc: datetime,
    max_text_codepoints: int,
) -> BrowserJobObservationV1:
    body = detail.body
    reasons: list[ObservationReasonCode] = []
    truncated = detail.body_truncated or detail.collapsed_content
    if body is not None and len(body) > max_text_codepoints:
        body = body[:max_text_codepoints]
        truncated = True
        reasons.append(ObservationReasonCode.TEXT_LIMIT_REACHED)
    if (
        detail.body_truncated
        or detail.collapsed_content
        or detail.body_visibility is FakeBodyVisibility.PARTIAL
    ):
        truncated = True
        reasons.append(ObservationReasonCode.TEXT_PARTIAL)
    elif detail.body_visibility is FakeBodyVisibility.UNKNOWN:
        reasons.append(ObservationReasonCode.TEXT_COMPLETENESS_UNKNOWN)

    if body is None or not body:
        reasons.append(
            ObservationReasonCode.IMAGE_ONLY
            if detail.image_only
            else ObservationReasonCode.TEXT_UNAVAILABLE
        )
    if detail.author_marker is None:
        reasons.append(ObservationReasonCode.AUTHOR_UNAVAILABLE)
    if detail.post_kind is ObservedPostKind.UNKNOWN:
        reasons.append(ObservationReasonCode.POST_KIND_UNKNOWN)
    if detail.post_kind in (ObservedPostKind.QUOTE, ObservedPostKind.REPOST) and (
        detail.quoted_post_url is None or detail.quoted_author_label is None
    ):
        reasons.append(ObservationReasonCode.RELATIONSHIP_UNAVAILABLE)
    if detail.post_kind is ObservedPostKind.REPLY and (
        detail.parent_post_url is None or detail.parent_author_label is None
    ):
        reasons.append(ObservationReasonCode.RELATIONSHIP_UNAVAILABLE)

    published_at = detail.published_at_utc
    if published_at is not None:
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            published_at = None
            reasons.append(ObservationReasonCode.TIMESTAMP_UNAVAILABLE)
        else:
            published_at = published_at.astimezone(UTC)

    if body is None or not body:
        text_completeness = FakeBodyVisibility.UNKNOWN
    elif truncated or detail.body_visibility is FakeBodyVisibility.PARTIAL:
        text_completeness = FakeBodyVisibility.PARTIAL
    elif detail.body_visibility is FakeBodyVisibility.UNKNOWN:
        text_completeness = FakeBodyVisibility.UNKNOWN
    else:
        text_completeness = FakeBodyVisibility.FULL_VISIBLE

    if (
        body is None
        or not body
        or text_completeness is FakeBodyVisibility.UNKNOWN
        or detail.post_kind is ObservedPostKind.UNKNOWN
    ):
        status = PostObservationStatus.UNKNOWN
    elif reasons:
        status = PostObservationStatus.PARTIAL
    else:
        status = PostObservationStatus.COMPLETE

    related_author_marker = None
    if detail.post_kind in (ObservedPostKind.QUOTE, ObservedPostKind.REPOST):
        related_author_marker = "UNVERIFIED" if detail.quoted_author_label is not None else None
    elif detail.post_kind is ObservedPostKind.REPLY:
        related_author_marker = "UNVERIFIED" if detail.parent_author_label is not None else None

    post_revision_id = None
    if body is not None:
        post_revision_id = _opaque_id(
            "bj02-rev",
            str(captured.account_uuid),
            captured.canonical_post_url,
            body,
        )
    observation_id = _opaque_id(
        "bj02-obs",
        str(captured.account_uuid),
        captured.canonical_post_url,
        capture_id,
        observed_at_utc.isoformat(),
        str(detail_index),
    )
    return BrowserJobObservationV1(
        schema_version=1,
        observation_id=observation_id,
        account_uuid=captured.account_uuid,
        canonical_post_url=captured.canonical_post_url,
        local_card_id=captured.local_card_id,
        observed_at_utc=observed_at_utc,
        author_label=detail.author_marker,
        visible_text=body,
        text_completeness=text_completeness,
        text_truncated=truncated,
        status=status,
        reason_codes=tuple(dict.fromkeys(reasons)),
        post_kind=detail.post_kind,
        published_at_utc=published_at,
        quoted_post_url=detail.quoted_post_url,
        quoted_author_label=detail.quoted_author_label,
        parent_post_url=detail.parent_post_url,
        parent_author_label=detail.parent_author_label,
        related_author_marker=related_author_marker,
        post_revision_id=post_revision_id,
    )


def _opaque_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _is_synthetic_post_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_TARGET_URL_LENGTH:
        return False
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "threads.example.invalid"
            and parsed.hostname == "threads.example.invalid"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and parsed.path.startswith("/")
            and parsed.path != "/"
            and not parsed.fragment
        )
    except ValueError:
        return False


def _fake_origin(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_TARGET_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port is not None:
            return None
        if not parsed.hostname:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return None
