"""Bounded read-only collection of replies on explicitly configured own Threads."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from threads_platform.api.contracts import (
    DiscoveryPage,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
)
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import (
    AccountNurtureInboundConfig,
    NurturePresetV1,
)
from threads_platform.standalone.nurture_store import (
    NurtureStateError,
    fingerprint_remote_reply,
    fingerprint_remote_thread,
)

_SAFE_SOURCE_CODES = frozenset(
    {
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "THREADS_API_REJECTED_REQUEST",
        "THREADS_AUTHENTICATION_FAILED",
        "THREADS_CREDENTIAL_EXPIRED",
        "THREADS_CREDENTIAL_INVALID",
        "THREADS_CREDENTIAL_NOT_CONFIGURED",
        "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        "THREADS_DOCUMENTATION_CONTRACT_MISMATCH",
        "THREADS_INVALID_REQUEST",
        "THREADS_OBJECT_NOT_FOUND",
        "THREADS_PERMISSION_DENIED",
        "THREADS_RATE_LIMITED",
        "THREADS_REAUTHORIZATION_REQUIRED",
        "THREADS_SERVER_ERROR",
        "THREADS_TRANSPORT_FAILURE",
    }
)
_PROFILE_STAGE = "profile_posts"
_CONVERSATION_STAGE = "conversation"


class NurtureInboundStatus(StrEnum):
    """Safe status for the optional account-owned inbound capability."""

    READY = "INBOUND_READY"
    NOT_CONFIGURED = "INBOUND_NOT_CONFIGURED"
    ACCOUNT_MISMATCH = "INBOUND_ACCOUNT_MISMATCH"
    OWNERSHIP_MISMATCH = "INBOUND_OWNERSHIP_MISMATCH"
    OWNERSHIP_UNVERIFIED = "INBOUND_OWNERSHIP_UNVERIFIED"
    PERMISSION_MISSING = "INBOUND_PERMISSION_MISSING"
    READ_FAILED = "INBOUND_READ_FAILED"
    CONFIGURATION_INVALID = "INBOUND_CONFIGURATION_INVALID"


@dataclass(frozen=True, slots=True, repr=False)
class NurtureInboundCandidate:
    """An ephemeral proven reply with repr-hidden IDs for explicit apply only."""

    fingerprint: str
    timestamp: datetime | None
    root_thread_id: str = field(repr=False)
    reply_id: str = field(repr=False)
    replied_to_id: str | None = field(repr=False)
    text: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "NurtureInboundCandidate("
            f"fingerprint={self.fingerprint[:12]}…, timestamp_known={self.timestamp is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NurtureInboundResult:
    """Bounded reply counts, readiness and ordered proven candidates."""

    discovered_count: int
    deduped_count: int
    candidates: tuple[NurtureInboundCandidate, ...]
    status: NurtureInboundStatus = NurtureInboundStatus.NOT_CONFIGURED

    def __repr__(self) -> str:
        return (
            "NurtureInboundResult("
            f"discovered_count={self.discovered_count}, deduped_count={self.deduped_count}, "
            f"candidate_count={len(self.candidates)}, status={self.status.value})"
        )


class NurtureConversationError(Exception):
    """A bounded source failure with no owner name, reply ID, or exception message."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = (
            code if type(code) is str and code in _SAFE_SOURCE_CODES else "DISCOVERY_SOURCE_FAILED"
        )
        self.stage = (
            stage
            if type(stage) is str and stage in {_PROFILE_STAGE, _CONVERSATION_STAGE}
            else _PROFILE_STAGE
        )
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureConversationError(code={self.code}, stage={self.stage})"


async def collect_nurture_inbound(
    api: LocalThreadsApiRuntime,
    account_alias: str,
    preset: NurturePresetV1,
    inbound_config: AccountNurtureInboundConfig | None = None,
) -> NurtureInboundResult:
    """Read replies only after the alias token proves the configured owner identity."""

    if inbound_config is None:
        return _empty_result(NurtureInboundStatus.NOT_CONFIGURED)
    if type(inbound_config) is not AccountNurtureInboundConfig:
        return _empty_result(NurtureInboundStatus.CONFIGURATION_INVALID)
    if inbound_config.account_alias != account_alias or inbound_config.preset_id != preset.id:
        return _empty_result(NurtureInboundStatus.ACCOUNT_MISMATCH)
    maximum_threads = inbound_config.max_owned_threads_inspected_per_explicit_run
    if maximum_threads == 0:
        return _empty_result(NurtureInboundStatus.NOT_CONFIGURED)

    try:
        identity = await api.authenticated_profile(account_alias)
    except Exception as error:  # noqa: BLE001
        status = _identity_failure_status(error)
        return _empty_result(status)
    if type(identity) is not RemoteAuthenticatedProfile:
        return _empty_result(NurtureInboundStatus.OWNERSHIP_UNVERIFIED)
    if identity.username.casefold() != inbound_config.owner_public_username.casefold():
        return _empty_result(NurtureInboundStatus.OWNERSHIP_MISMATCH)
    owner_username = identity.username

    profile_limit = min(preset.per_source_page_limit, maximum_threads)
    try:
        profile_page = await api.profile_posts(
            account_alias,
            owner_username,
            after=None,
            limit=profile_limit,
        )
    except Exception as error:  # noqa: BLE001
        # API adapters may raise transport-specific exceptions; expose only a safe status.
        return _empty_result(_read_failure_status(error))
    if type(profile_page) is not DiscoveryPage or type(profile_page.threads) is not tuple:
        return _empty_result(NurtureInboundStatus.READ_FAILED)

    try:
        own_thread_ids = _unique_thread_prefix(profile_page, maximum_threads)
    except NurtureConversationError:
        return _empty_result(NurtureInboundStatus.READ_FAILED)
    reply_occurrences = 0
    reply_fingerprints: set[str] = set()
    proven_by_fingerprint: dict[str, NurtureInboundCandidate] = {}

    for own_thread_id in own_thread_ids:
        try:
            page = await api.conversation(account_alias, own_thread_id, after=None)
        except Exception as error:  # noqa: BLE001
            # API adapters may raise transport-specific exceptions; expose only a safe status.
            return _empty_result(_read_failure_status(error))
        if type(page) is not ReplyPage or type(page.replies) is not tuple:
            return _empty_result(NurtureInboundStatus.READ_FAILED)

        # Conversation has no caller-supplied page limit in the accepted shared API.
        # Inspect only this deterministic prefix to keep receipt and candidate work bounded.
        for reply in page.replies[: preset.per_source_page_limit]:
            if type(reply) is not RemoteReply:
                return _empty_result(NurtureInboundStatus.READ_FAILED)
            reply_occurrences += 1
            if reply.is_reply_owned_by_me is not None and (
                type(reply.is_reply_owned_by_me) is not bool
            ):
                return _empty_result(NurtureInboundStatus.READ_FAILED)
            try:
                fingerprint = fingerprint_remote_reply(reply.reply_id)
            except NurtureStateError:
                continue
            reply_fingerprints.add(fingerprint)

            if reply.is_reply_owned_by_me is not False or reply.root_post_id != own_thread_id:
                continue

            candidate = NurtureInboundCandidate(
                fingerprint=fingerprint,
                timestamp=_parse_reply_timestamp(reply.timestamp),
                root_thread_id=own_thread_id,
                reply_id=reply.reply_id,
                replied_to_id=reply.replied_to_id,
                text=reply.text,
            )
            # The first proven occurrence keeps its timestamp; repeated hits never
            # improve a candidate's relative priority.
            if fingerprint not in proven_by_fingerprint:
                proven_by_fingerprint[fingerprint] = candidate

    candidates = tuple(sorted(proven_by_fingerprint.values(), key=_inbound_sort_key))
    return NurtureInboundResult(
        discovered_count=reply_occurrences,
        deduped_count=len(reply_fingerprints),
        candidates=candidates,
        status=NurtureInboundStatus.READY,
    )


def _empty_result(status: NurtureInboundStatus) -> NurtureInboundResult:
    return NurtureInboundResult(0, 0, (), status)


def _identity_failure_status(error: Exception) -> NurtureInboundStatus:
    return (
        NurtureInboundStatus.PERMISSION_MISSING
        if _safe_error_code(error) == "THREADS_PERMISSION_DENIED"
        else NurtureInboundStatus.OWNERSHIP_UNVERIFIED
    )


def _read_failure_status(error: Exception) -> NurtureInboundStatus:
    return (
        NurtureInboundStatus.PERMISSION_MISSING
        if _safe_error_code(error) == "THREADS_PERMISSION_DENIED"
        else NurtureInboundStatus.READ_FAILED
    )


def _safe_error_code(error: Exception) -> str | None:
    try:
        code = getattr(error, "code", None)
    except Exception:  # noqa: BLE001
        return None
    return code if type(code) is str and code in _SAFE_SOURCE_CODES else None


def _unique_thread_prefix(page: DiscoveryPage, maximum_threads: int) -> tuple[str, ...]:
    selected: list[str] = []
    seen_fingerprints: set[str] = set()
    for thread in page.threads:
        if type(thread) is not RemoteDiscoveryThread:
            raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _PROFILE_STAGE)
        if type(thread.remote_thread_id) is not str:
            continue
        normalized = unicodedata.normalize("NFC", thread.remote_thread_id.strip())
        try:
            fingerprint = fingerprint_remote_thread(normalized)
        except NurtureStateError:
            continue
        if fingerprint in seen_fingerprints:
            continue
        selected.append(normalized)
        seen_fingerprints.add(fingerprint)
        if len(selected) >= maximum_threads:
            break
    return tuple(selected)


def _parse_reply_timestamp(value: str | None) -> datetime | None:
    if type(value) is not str or not value or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except OverflowError, TypeError, ValueError:
        return None


def _inbound_sort_key(
    candidate: NurtureInboundCandidate,
) -> tuple[int, int, str]:
    timestamp = candidate.timestamp
    if timestamp is None:
        return (1, 0, candidate.fingerprint)
    epoch_microseconds = (
        (timestamp.toordinal() - 1) * 86_400_000_000
        + timestamp.hour * 3_600_000_000
        + timestamp.minute * 60_000_000
        + timestamp.second * 1_000_000
        + timestamp.microsecond
    )
    return (0, -epoch_microseconds, candidate.fingerprint)
