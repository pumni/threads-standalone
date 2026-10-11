"""One bounded Nurture run with observe-only default and explicit apply inputs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from threads_platform.api.contracts import PublishingQuota
from threads_platform.standalone.accounts import LocalAccount
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.nurture import (
    AccountNurtureInboundConfig,
    NurturePresetV1,
    nurture_quote_allowed,
)
from threads_platform.standalone.nurture_content import ContentCandidateV1
from threads_platform.standalone.nurture_conversations import (
    NurtureConversationError,
    NurtureInboundCandidate,
    NurtureInboundStatus,
    collect_nurture_inbound,
)
from threads_platform.standalone.nurture_discovery import (
    NurtureCandidate,
    NurtureDiscoveryError,
    NurtureDiscoverySource,
    discover_nurture_candidates,
)
from threads_platform.standalone.nurture_draft import NurtureDraft
from threads_platform.standalone.nurture_quote_draft import NurtureQuoteDraft
from threads_platform.standalone.nurture_store import (
    NurtureAccountLock,
    NurtureContentRecordV1,
    NurtureRunScope,
    NurtureRunV1,
    NurtureStateError,
    NurtureStore,
    NurtureTargetStateV1,
    NurtureTargetV1,
)

_SAFE_RUNNER_CODES = frozenset(
    {
        "ACCOUNT_BUSY",
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_MEDIA_ID",
        "INVALID_NURTURE_DRAFT",
        "INVALID_NURTURE_QUOTE_DRAFT",
        "INVALID_NURTURE_CONTENT",
        "INVALID_NURTURE_PRESET",
        "INVALID_QUERY",
        "INVALID_REPLY_ID",
        "INVALID_REPLY_TEXT",
        "INVALID_SEARCH_MODE",
        "INVALID_SEARCH_TYPE",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "INBOUND_ACCOUNT_MISMATCH",
        "INBOUND_CONFIGURATION_INVALID",
        "INBOUND_OWNERSHIP_MISMATCH",
        "INBOUND_OWNERSHIP_UNVERIFIED",
        "INBOUND_PERMISSION_MISSING",
        "INBOUND_READ_FAILED",
        "LOCAL_OPERATION_UNAVAILABLE",
        "NURTURE_BUSY",
        "NURTURE_DRAFT_TARGET_MISMATCH",
        "NURTURE_REPLY_APPLY_DISABLED",
        "NURTURE_QUOTE_APPLY_DISABLED",
        "NURTURE_QUOTE_TARGET_MISMATCH",
        "NURTURE_CONTENT_ACCOUNT_MISMATCH",
        "NURTURE_CONTENT_APPLY_DISABLED",
        "NURTURE_CONTENT_PROVENANCE_CONFLICT",
        "NURTURE_CONTENT_DRAFT_REUSE_CONFLICT",
        "NURTURE_CONTENT_ALREADY_USED",
        "NURTURE_CONTENT_UNRESOLVED",
        "NURTURE_INPUT_CONFLICT",
        "NURTURE_RUN_NOT_FOUND",
        "NURTURE_STATE_CAP_REACHED",
        "NURTURE_STATE_INVALID",
        "NURTURE_TARGET_AMBIGUOUS",
        "NURTURE_TARGET_PENDING",
        "NURTURE_TARGET_RESERVED",
        "NURTURE_VERSION_UNSUPPORTED",
        "OPERATION_CANCELLED",
        "OPERATION_STATE_INVALID",
        "PRESET_NOT_FOUND",
        "PUBLISH_OUTCOME_AMBIGUOUS",
        "RUN_FAILED",
        "THREADS_API_REJECTED_REQUEST",
        "THREADS_AUTHENTICATION_FAILED",
        "THREADS_CONTAINER_ERROR",
        "THREADS_CONTAINER_EXPIRED",
        "THREADS_CONTAINER_PROCESSING_TIMEOUT",
        "THREADS_CREDENTIAL_EXPIRED",
        "THREADS_CREDENTIAL_INVALID",
        "THREADS_CREDENTIAL_NOT_CONFIGURED",
        "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        "THREADS_DOCUMENTATION_CONTRACT_MISMATCH",
        "THREADS_INVALID_REQUEST",
        "THREADS_OBJECT_NOT_FOUND",
        "THREADS_PERMISSION_DENIED",
        "THREADS_PUBLISHING_QUOTA_REACHED",
        "THREADS_RATE_LIMITED",
        "THREADS_REAUTHORIZATION_REQUIRED",
        "THREADS_REPLY_QUOTA_REACHED",
        "THREADS_SERVER_ERROR",
        "THREADS_TRANSPORT_FAILURE",
    }
)
_SAFE_STAGE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_PREVIEW_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PREVIEW_TEXT_CHARS = 500


@dataclass(frozen=True, slots=True, repr=False)
class NurturePreview:
    """Bounded in-memory preview; remote and proposed text never enter repr or receipts."""

    target_fingerprint: str
    source: NurtureDiscoverySource | Literal["INBOUND_REPLY"]
    reason_codes: tuple[str, ...]
    action: Literal["OBSERVE", "REPLY_RECOMMENDED", "REPLY", "QUOTE"]
    target_text: str | None = field(repr=False)
    proposed_text: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.target_fingerprint) is not str
            or _PREVIEW_FINGERPRINT.fullmatch(self.target_fingerprint) is None
            or self.source not in {*NurtureDiscoverySource, "INBOUND_REPLY"}
            or type(self.reason_codes) is not tuple
            or len(self.reason_codes) > 16
            or any(
                type(code) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", code) is None
                for code in self.reason_codes
            )
            or self.action not in {"OBSERVE", "REPLY_RECOMMENDED", "REPLY", "QUOTE"}
        ):
            raise ValueError("invalid nurture preview")
        for name in ("target_text", "proposed_text"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise ValueError("invalid nurture preview")
            if value is not None and len(value) > _MAX_PREVIEW_TEXT_CHARS:
                object.__setattr__(self, name, value[:_MAX_PREVIEW_TEXT_CHARS])

    def __repr__(self) -> str:
        return (
            "NurturePreview("
            f"target_fingerprint={self.target_fingerprint[:12]}…, "
            f"source={self.source}, reason_codes={self.reason_codes!r}, "
            f"action={self.action}, target_text=<redacted>, proposed_text=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NurtureRunResult:
    """A receipt plus the safe fingerprint selected in this invocation."""

    receipt: NurtureRunV1
    target_fingerprint: str | None
    content_candidate_id: UUID | None = None
    inbound_status: NurtureInboundStatus = NurtureInboundStatus.NOT_CONFIGURED
    preview: NurturePreview | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "NurtureRunResult("
            f"run_id={self.receipt.id}, outcome={self.receipt.outcome}, "
            f"target_fingerprint={self.target_fingerprint or '-'}, "
            f"content_candidate_id={self.content_candidate_id or '-'}, "
            f"inbound_status={self.inbound_status.value})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _NurtureSelection:
    candidate: NurtureInboundCandidate | NurtureCandidate | None
    target_by_fingerprint: dict[str, NurtureTargetV1]
    discovered_count: int
    deduped_count: int
    inbound_status: NurtureInboundStatus
    preview: NurturePreview | None = None


class NurtureRunnerError(Exception):
    """A bounded CLI-safe error that never carries remote or credential data."""

    def __init__(
        self,
        code: str,
        run_id: UUID | None = None,
        operation_id: UUID | None = None,
    ) -> None:
        self.code = code if code in _SAFE_RUNNER_CODES else "RUN_FAILED"
        self.run_id = run_id
        self.operation_id = operation_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        operation = f", operation_id={self.operation_id}" if self.operation_id else ""
        return f"NurtureRunnerError(code={self.code}, run_id={self.run_id}{operation})"


class NurtureRunnerInterrupted(KeyboardInterrupt):
    """Keyboard interruption with a safe local receipt reference for CLI output."""

    code = "INTERRUPTED"

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureRunnerInterrupted(run_id={self.run_id})"


class NurtureRunner:
    """Compose accepted discovery policy with local receipts and optional explicit apply."""

    def __init__(self, api: LocalThreadsApiRuntime, store: NurtureStore) -> None:
        self._api = api
        self._store = store

    async def run(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
        apply_requested: bool = False,
        draft: NurtureDraft | None = None,
        quote_draft: NurtureQuoteDraft | None = None,
        content_candidate: ContentCandidateV1 | None = None,
        mutations: LocalThreadsMutationRuntime | None = None,
        inbound_config: AccountNurtureInboundConfig | None = None,
        preview_only: bool = False,
    ) -> NurtureRunV1:
        return (
            await self.run_with_selection(
                account,
                preset,
                now=now,
                apply_requested=apply_requested,
                draft=draft,
                quote_draft=quote_draft,
                content_candidate=content_candidate,
                mutations=mutations,
                inbound_config=inbound_config,
                preview_only=preview_only,
            )
        ).receipt

    async def run_with_selection(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
        apply_requested: bool = False,
        draft: NurtureDraft | None = None,
        quote_draft: NurtureQuoteDraft | None = None,
        content_candidate: ContentCandidateV1 | None = None,
        mutations: LocalThreadsMutationRuntime | None = None,
        inbound_config: AccountNurtureInboundConfig | None = None,
        preview_only: bool = False,
    ) -> NurtureRunResult:
        if type(account) is not LocalAccount or type(account.id) is not UUID:
            raise NurtureRunnerError("RUN_FAILED")
        if type(preset) is not NurturePresetV1:
            raise NurtureRunnerError("PRESET_NOT_FOUND")
        if type(apply_requested) is not bool:
            raise NurtureRunnerError("RUN_FAILED")
        if type(preview_only) is not bool:
            raise NurtureRunnerError("RUN_FAILED")
        if draft is not None and type(draft) is not NurtureDraft:
            raise NurtureRunnerError("INVALID_NURTURE_DRAFT")
        if quote_draft is not None and type(quote_draft) is not NurtureQuoteDraft:
            raise NurtureRunnerError("INVALID_NURTURE_QUOTE_DRAFT")
        if content_candidate is not None and type(content_candidate) is not ContentCandidateV1:
            raise NurtureRunnerError("INVALID_NURTURE_CONTENT")
        if content_candidate is not None and (
            content_candidate.account_alias != account.alias
            or content_candidate.preset_id != preset.id
        ):
            raise NurtureRunnerError("NURTURE_CONTENT_ACCOUNT_MISMATCH")
        provided_inputs = sum(item is not None for item in (draft, quote_draft, content_candidate))
        if provided_inputs > 1:
            raise NurtureRunnerError("NURTURE_INPUT_CONFLICT")
        if preview_only and (
            apply_requested
            or draft is None
            or quote_draft is not None
            or content_candidate is not None
        ):
            raise NurtureRunnerError("INVALID_NURTURE_DRAFT")
        if quote_draft is not None and not apply_requested:
            raise NurtureRunnerError("INVALID_NURTURE_QUOTE_DRAFT")
        if quote_draft is not None and not nurture_quote_allowed(preset):
            raise NurtureRunnerError("NURTURE_QUOTE_APPLY_DISABLED")
        expects_mutations = (
            (draft is not None and not preview_only)
            or quote_draft is not None
            or (content_candidate is not None and apply_requested)
        )
        if (expects_mutations != (mutations is not None)) or (
            draft is not None and not (apply_requested or preview_only)
        ):
            raise NurtureRunnerError("INVALID_NURTURE_DRAFT")

        captured_now = _capture_utc(now)
        owner = self._store.acquire_account_lock(account.id)
        run_id: UUID | None = None
        try:
            async with owner:
                scope = owner.start_run(preset, now=captured_now)
                run_id = scope.receipt.id
                try:
                    return await self._run_started(
                        owner,
                        scope,
                        account,
                        preset,
                        captured_now,
                        apply_requested=apply_requested,
                        draft=draft,
                        quote_draft=quote_draft,
                        content_candidate=content_candidate,
                        mutations=mutations,
                        inbound_config=inbound_config,
                        preview_only=preview_only,
                    )
                except KeyboardInterrupt:
                    raise NurtureRunnerInterrupted(run_id) from None
        except NurtureRunnerError:
            raise
        except NurtureStateError as error:
            raise NurtureRunnerError(error.code, run_id) from None
        raise NurtureRunnerError("RUN_FAILED", run_id) from None

    async def _run_started(
        self,
        owner: NurtureAccountLock,
        scope: NurtureRunScope,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
        *,
        apply_requested: bool,
        draft: NurtureDraft | None,
        quote_draft: NurtureQuoteDraft | None,
        content_candidate: ContentCandidateV1 | None,
        mutations: LocalThreadsMutationRuntime | None,
        inbound_config: AccountNurtureInboundConfig | None,
        preview_only: bool,
    ) -> NurtureRunResult:
        run_id = scope.receipt.id
        stage = "selection"
        selection: _NurtureSelection | None = None
        async with scope:
            try:
                selection = await self._select_current_candidate(
                    owner,
                    account,
                    preset,
                    now,
                    draft,
                    quote_draft,
                    inbound_config,
                    apply_requested,
                    content_candidate is not None,
                )
                chosen = selection.candidate
                selected_count = 1 if chosen is not None else 0
                engagement_decision: str | None = None
                if chosen is not None:
                    decision = (
                        "QUOTE_APPLY"
                        if quote_draft is not None
                        else _selection_decision(
                            chosen,
                            apply_requested,
                            draft,
                            own_content_requested=content_candidate is not None,
                            preview_only=preview_only,
                        )
                    )
                    engagement_decision = decision
                    stage = "receipt"
                    scope.update(
                        discovered_count=selection.discovered_count,
                        deduped_count=selection.deduped_count,
                        selected_count=selected_count,
                        enriched_count=0,
                        replied_count=0,
                        published_count=0,
                        skipped_count=selection.deduped_count - selected_count,
                        decision_codes=(decision,),
                    )
                    selected_fingerprint = chosen.fingerprint
                    if preview_only:
                        return NurtureRunResult(
                            scope.finish("SUCCESS", now=now),
                            selected_fingerprint,
                            inbound_status=selection.inbound_status,
                            preview=selection.preview,
                        )
                    if quote_draft is not None:
                        stage = "quote"
                        return await self._apply_quote(
                            owner,
                            scope,
                            account,
                            preset,
                            now,
                            run_id,
                            chosen,
                            selection,
                            quote_draft,
                            mutations,
                        )
                    if draft is not None:
                        if selected_fingerprint != draft.target_fingerprint or mutations is None:
                            raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
                        thread_id, parent_reply_id = _mutation_target(chosen)
                        stage = "quota"
                        quota = await self._api.quota(account.alias)
                        if _reply_quota_exhausted(quota):
                            raise NurtureRunnerError("THREADS_REPLY_QUOTA_REACHED")
                        stage = "reserve"
                        owner.reserve_target(
                            preset,
                            selected_fingerprint,
                            run_id,
                            "REPLY_APPLY",
                            now=now,
                        )
                        stage = "publish"
                        result = await mutations.create_reply(
                            account.alias,
                            thread_id,
                            draft.text,
                            parent_reply_id=parent_reply_id,
                        )
                        if (
                            type(result) is not CreatedReplyResult
                            or type(result.operation_id) is not UUID
                            or result.operation_id.version != 4
                        ):
                            raise NurtureRunnerError("RUN_FAILED")

                        stage = "confirm"
                        owner.complete_target_action(
                            preset,
                            selected_fingerprint,
                            run_id,
                            "CONFIRMED",
                            result.operation_id,
                            now=now,
                        )
                        scope.link_operation(result.operation_id)
                        scope.update(replied_count=1, decision_codes=("REPLY_APPLIED",))
                        return NurtureRunResult(
                            scope.finish("SUCCESS", now=now),
                            selected_fingerprint,
                            inbound_status=selection.inbound_status,
                            preview=selection.preview,
                        )

                    stage = "observe"
                    observed = owner.observe_target(
                        preset,
                        selected_fingerprint,
                        run_id,
                        decision,
                        now=now,
                    )
                    previous = selection.target_by_fingerprint.get(selected_fingerprint)
                    if previous is not None and not _action_history_preserved(previous, observed):
                        raise NurtureStateError("NURTURE_STATE_INVALID")
                    if observed.action_state not in {"NONE", "CONFIRMED"}:
                        raise NurtureStateError("NURTURE_STATE_INVALID")
                    if content_candidate is None:
                        return NurtureRunResult(
                            scope.finish("SUCCESS", now=now),
                            selected_fingerprint,
                            inbound_status=selection.inbound_status,
                            preview=selection.preview,
                        )

                if content_candidate is None:
                    stage = "receipt"
                    scope.update(
                        discovered_count=selection.discovered_count,
                        deduped_count=selection.deduped_count,
                        selected_count=0,
                        enriched_count=0,
                        replied_count=0,
                        published_count=0,
                        skipped_count=selection.deduped_count,
                        decision_codes=("NO_ACTION",),
                    )
                    return NurtureRunResult(
                        scope.finish("SUCCESS", now=now),
                        None,
                        inbound_status=selection.inbound_status,
                    )

                def set_stage(value: str) -> None:
                    nonlocal stage
                    stage = value

                return await self._run_own_content(
                    owner,
                    scope,
                    account,
                    preset,
                    now,
                    run_id,
                    selection,
                    content_candidate,
                    apply_requested,
                    mutations,
                    engagement_decision,
                    set_stage,
                )
            except KeyboardInterrupt, asyncio.CancelledError:
                raise
            except NurtureDiscoveryError as error:
                failed_stage = _safe_stage(error.stage, "discovery")
                _finish_failed(scope, error.code, failed_stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except NurtureConversationError as error:
                _finish_failed(scope, error.code, error.stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except StandaloneMutationError as error:
                if (
                    error.code == "PUBLISH_OUTCOME_AMBIGUOUS"
                    and type(error.operation_id) is UUID
                    and error.operation_id.version == 4
                ):
                    if content_candidate is not None:
                        _best_effort_content_ambiguous(
                            owner,
                            scope,
                            preset,
                            content_candidate,
                            run_id,
                            error.operation_id,
                            now,
                        )
                    else:
                        _best_effort_ambiguous(
                            owner,
                            scope,
                            preset,
                            None
                            if selection is None or selection.candidate is None
                            else selection.candidate.fingerprint,
                            run_id,
                            error.operation_id,
                            now,
                        )
                    raise NurtureRunnerError(
                        "PUBLISH_OUTCOME_AMBIGUOUS", run_id, error.operation_id
                    ) from None
                code = _safe_code(error.code)
                _finish_failed(scope, code, stage, run_id)
                raise NurtureRunnerError(code, run_id) from None
            except NurtureRunnerError as error:
                _finish_failed(scope, error.code, stage, run_id)
                raise NurtureRunnerError(error.code, run_id, error.operation_id) from None
            except NurtureStateError as error:
                _finish_failed(scope, error.code, stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except Exception as error:  # noqa: BLE001
                # Convert arbitrary API/runtime failures to a durable, bounded receipt outcome.
                code = _safe_code(_exception_code(error))
                _finish_failed(scope, code, stage, run_id)
                raise NurtureRunnerError(code, run_id) from None
        raise NurtureRunnerError("RUN_FAILED", run_id) from None

    async def _run_own_content(
        self,
        owner: NurtureAccountLock,
        scope: NurtureRunScope,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
        run_id: UUID,
        selection: _NurtureSelection,
        content_candidate: ContentCandidateV1,
        apply_requested: bool,
        mutations: LocalThreadsMutationRuntime | None,
        engagement_decision: str | None,
        set_stage: Callable[[str], None],
    ) -> NurtureRunResult:
        """Evaluate and, only when separately approved, publish own content."""

        set_stage("content_policy")
        record = owner.register_content_candidate(
            preset,
            content_candidate.candidate_id,
            content_candidate.source_fingerprint,
            content_candidate.draft_fingerprint,
            content_candidate.category,
            run_id,
            now=now,
        )
        content_state = owner.get_content_state(preset)
        is_due = _own_content_is_due(content_state.candidates, preset, now)
        discovered_count = selection.discovered_count + 1
        deduped_count = selection.deduped_count + 1
        selected_count = 1 if is_due or selection.candidate is not None else 0
        content_decision = (
            "CONTENT_NOT_DUE"
            if not is_due
            else "CONTENT_APPLY"
            if apply_requested
            else "CONTENT_RECOMMENDED_NO_APPLY"
        )
        set_stage("receipt")
        scope.update(
            discovered_count=discovered_count,
            deduped_count=deduped_count,
            selected_count=selected_count,
            enriched_count=0,
            replied_count=0,
            published_count=0,
            skipped_count=deduped_count - selected_count,
            decision_codes=(
                (content_decision,)
                if engagement_decision is None
                else (engagement_decision, content_decision)
            ),
        )
        target_fingerprint = (
            None if selection.candidate is None else selection.candidate.fingerprint
        )
        if not is_due or not apply_requested:
            return NurtureRunResult(
                scope.finish("SUCCESS", now=now),
                target_fingerprint,
                record.candidate_id,
                selection.inbound_status,
                selection.preview,
            )

        set_stage("policy")
        if not preset.engagement_enabled or preset.max_own_post_mutations_per_explicit_run < 1:
            raise NurtureRunnerError("NURTURE_CONTENT_APPLY_DISABLED")
        if mutations is None:
            raise NurtureRunnerError("RUN_FAILED")
        set_stage("quota")
        quota = await self._api.quota(account.alias)
        if _publishing_quota_exhausted(quota):
            raise NurtureRunnerError("THREADS_PUBLISHING_QUOTA_REACHED")

        set_stage("reserve")
        owner.reserve_content_candidate(
            preset,
            content_candidate.source_fingerprint,
            content_candidate.draft_fingerprint,
            run_id,
        )
        set_stage("publish")
        result = await mutations.publish_text(account.alias, content_candidate.text)
        if (
            type(result) is not PublishedTextResult
            or type(result.operation_id) is not UUID
            or result.operation_id.version != 4
        ):
            raise NurtureRunnerError("RUN_FAILED")

        set_stage("confirm")
        owner.complete_content_candidate(
            preset,
            content_candidate.source_fingerprint,
            content_candidate.draft_fingerprint,
            run_id,
            "PUBLISHED",
            result.operation_id,
            now=now,
        )
        scope.link_operation(result.operation_id)
        scope.update(
            published_count=1,
            decision_codes=(
                *scope.receipt.decision_codes[:-1],
                "CONTENT_PUBLISHED",
            ),
        )
        return NurtureRunResult(
            scope.finish("SUCCESS", now=now),
            target_fingerprint,
            record.candidate_id,
            selection.inbound_status,
            selection.preview,
        )

    async def _apply_quote(
        self,
        owner: NurtureAccountLock,
        scope: NurtureRunScope,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
        run_id: UUID,
        candidate: NurtureInboundCandidate | NurtureCandidate,
        selection: _NurtureSelection,
        quote_draft: NurtureQuoteDraft,
        mutations: LocalThreadsMutationRuntime | None,
    ) -> NurtureRunResult:
        fingerprint = candidate.fingerprint
        if (
            not isinstance(candidate, NurtureCandidate)
            or fingerprint != quote_draft.target_fingerprint
            or mutations is None
        ):
            raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")
        previous = selection.target_by_fingerprint.get(fingerprint)
        if previous is not None and previous.action_state != "NONE":
            raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")

        quota = await self._api.quota(account.alias)
        if _publishing_quota_exhausted(quota):
            raise NurtureRunnerError("THREADS_PUBLISHING_QUOTA_REACHED")
        owner.reserve_target(preset, fingerprint, run_id, "QUOTE_APPLY", now=now)
        result = await mutations.publish_quote(
            account.alias,
            candidate.remote_thread_id,
            quote_draft.text,
        )
        if (
            type(result) is not PublishedTextResult
            or type(result.operation_id) is not UUID
            or result.operation_id.version != 4
        ):
            raise NurtureRunnerError("RUN_FAILED")

        owner.complete_target_action(
            preset,
            fingerprint,
            run_id,
            "CONFIRMED",
            result.operation_id,
            now=now,
        )
        scope.link_operation(result.operation_id)
        scope.update(published_count=1, decision_codes=("QUOTE_PUBLISHED",))
        return NurtureRunResult(
            scope.finish("SUCCESS", now=now),
            fingerprint,
            inbound_status=selection.inbound_status,
            preview=selection.preview,
        )

    async def _select_current_candidate(
        self,
        owner: NurtureAccountLock,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
        draft: NurtureDraft | None,
        quote_draft: NurtureQuoteDraft | None,
        inbound_config: AccountNurtureInboundConfig | None,
        apply_requested: bool,
        own_content_requested: bool,
    ) -> _NurtureSelection:
        state = NurtureTargetStateV1(
            version=1,
            account_id=account.id,
            preset_id=preset.id,
            targets=owner.get_targets(preset),
        )
        targets = {target.fingerprint: target for target in state.targets}
        bound_fingerprint = (
            draft.target_fingerprint
            if draft is not None
            else quote_draft.target_fingerprint
            if quote_draft is not None
            else None
        )
        bound_target = None if bound_fingerprint is None else targets.get(bound_fingerprint)
        if draft is not None:
            if not preset.engagement_enabled or preset.max_replies_per_explicit_run < 1:
                raise NurtureRunnerError("NURTURE_REPLY_APPLY_DISABLED")
            if bound_target is not None and bound_target.action_state != "NONE":
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
        if quote_draft is not None:
            if not nurture_quote_allowed(preset):
                raise NurtureRunnerError("NURTURE_QUOTE_APPLY_DISABLED")
            if bound_target is not None and bound_target.action_state != "NONE":
                raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")

        inbound = await collect_nurture_inbound(self._api, account.alias, preset, inbound_config)
        if inbound.status not in {
            NurtureInboundStatus.READY,
            NurtureInboundStatus.NOT_CONFIGURED,
        }:
            raise NurtureRunnerError(inbound.status.value)
        if quote_draft is not None and any(
            candidate.fingerprint == quote_draft.target_fingerprint
            for candidate in inbound.candidates
        ):
            raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")
        if draft is not None:
            inbound_bound = next(
                (
                    candidate
                    for candidate in inbound.candidates
                    if candidate.fingerprint == draft.target_fingerprint
                ),
                None,
            )
            if inbound_bound is not None and not _candidate_surface_eligible(
                inbound_bound.fingerprint,
                targets.get(inbound_bound.fingerprint),
                preset.seen_cooldown_seconds,
                now,
                draft,
                None,
            ):
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
            chosen: NurtureInboundCandidate | NurtureCandidate | None = inbound_bound
        elif quote_draft is None:
            chosen = next(
                (
                    candidate
                    for candidate in inbound.candidates
                    if _candidate_surface_eligible(
                        candidate.fingerprint,
                        targets.get(candidate.fingerprint),
                        preset.seen_cooldown_seconds,
                        now,
                        None,
                        None,
                    )
                ),
                None,
            )
        else:
            chosen = None
        discovered_count = inbound.discovered_count
        deduped_count = inbound.deduped_count
        if chosen is not None:
            if draft is not None and chosen.fingerprint != draft.target_fingerprint:
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
            if quote_draft is not None and (
                not isinstance(chosen, NurtureCandidate)
                or chosen.fingerprint != quote_draft.target_fingerprint
            ):
                raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")
        else:
            discovery_state = _discovery_state_for_bound_target(
                state,
                bound_target,
                bound_fingerprint,
            )
            if bound_fingerprint is None:
                discovery = await discover_nurture_candidates(
                    self._api,
                    account.alias,
                    preset,
                    discovery_state,
                    now,
                )
            else:
                discovery = await discover_nurture_candidates(
                    self._api,
                    account.alias,
                    preset,
                    discovery_state,
                    now,
                    exact_target_fingerprint=bound_fingerprint,
                )
            discovered_count += discovery.discovered_count
            deduped_count += discovery.deduped_count
            if bound_fingerprint is not None:
                chosen = next(
                    (
                        candidate
                        for candidate in discovery.selected_candidates
                        if candidate.fingerprint == bound_fingerprint
                        and _candidate_surface_eligible(
                            candidate.fingerprint,
                            targets.get(candidate.fingerprint),
                            preset.seen_cooldown_seconds,
                            now,
                            draft,
                            quote_draft,
                        )
                    ),
                    None,
                )
            else:
                mentions = tuple(
                    candidate
                    for candidate in discovery.selected_candidates
                    if candidate.source_class is NurtureDiscoverySource.MENTIONS
                )
                remaining = tuple(
                    candidate
                    for candidate in discovery.selected_candidates
                    if candidate.source_class is not NurtureDiscoverySource.MENTIONS
                )
                chosen = next(
                    (
                        candidate
                        for candidate in (*mentions, *remaining)
                        if _candidate_surface_eligible(
                            candidate.fingerprint,
                            targets.get(candidate.fingerprint),
                            preset.seen_cooldown_seconds,
                            now,
                            None,
                            None,
                        )
                    ),
                    None,
                )
            if draft is not None and (
                chosen is None or chosen.fingerprint != draft.target_fingerprint
            ):
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
            if quote_draft is not None and (
                chosen is None or chosen.fingerprint != quote_draft.target_fingerprint
            ):
                raise NurtureRunnerError("NURTURE_QUOTE_TARGET_MISMATCH")
        return _NurtureSelection(
            chosen,
            targets,
            discovered_count,
            deduped_count,
            inbound.status,
            _selection_preview(
                chosen,
                draft,
                quote_draft,
                apply_requested=apply_requested,
                own_content_requested=own_content_requested,
            ),
        )


def _capture_utc(value: datetime | None) -> datetime:
    captured = datetime.now(UTC) if value is None else value
    if type(captured) is not datetime or captured.tzinfo is None:
        raise NurtureRunnerError("RUN_FAILED")
    try:
        if captured.utcoffset() is None:
            raise NurtureRunnerError("RUN_FAILED")
        return captured.astimezone(UTC)
    except OverflowError, TypeError, ValueError:
        raise NurtureRunnerError("RUN_FAILED") from None


def _candidate_surface_eligible(
    fingerprint: str,
    target: NurtureTargetV1 | None,
    cooldown_seconds: int,
    now: datetime,
    draft: NurtureDraft | None,
    quote_draft: NurtureQuoteDraft | None,
) -> bool:
    bound_fingerprint = (
        draft.target_fingerprint
        if draft is not None
        else quote_draft.target_fingerprint
        if quote_draft is not None
        else None
    )
    if fingerprint == bound_fingerprint and _can_bypass_seen_cooldown(target, now):
        return True
    return _surface_eligible(target, cooldown_seconds, now)


def _selection_preview(
    candidate: NurtureInboundCandidate | NurtureCandidate | None,
    draft: NurtureDraft | None,
    quote_draft: NurtureQuoteDraft | None,
    *,
    apply_requested: bool,
    own_content_requested: bool,
) -> NurturePreview | None:
    if candidate is None:
        return None
    if isinstance(candidate, NurtureInboundCandidate):
        source: NurtureDiscoverySource | Literal["INBOUND_REPLY"] = "INBOUND_REPLY"
        reason_codes = ("INBOUND_REPLY",)
        target_text = candidate.text
    else:
        source = candidate.source_class
        reason_codes = candidate.reason_codes
        target_text = candidate.text
    if quote_draft is not None:
        action: Literal["OBSERVE", "REPLY_RECOMMENDED", "REPLY", "QUOTE"] = "QUOTE"
        proposed_text = quote_draft.text
    elif draft is not None:
        action = "REPLY"
        proposed_text = draft.text
    elif apply_requested and not own_content_requested:
        action = "REPLY_RECOMMENDED"
        proposed_text = None
    else:
        action = "OBSERVE"
        proposed_text = None
    return NurturePreview(
        target_fingerprint=candidate.fingerprint,
        source=source,
        reason_codes=reason_codes,
        action=action,
        target_text=target_text,
        proposed_text=proposed_text,
    )


def _can_bypass_seen_cooldown(target: NurtureTargetV1 | None, now: datetime) -> bool:
    if target is None:
        return True
    if target.action_state != "NONE":
        return False
    try:
        last_seen_at = target.last_seen_at
        if type(last_seen_at) is not datetime or last_seen_at.tzinfo is None:
            return False
        if last_seen_at.utcoffset() is None:
            return False
        return now >= last_seen_at.astimezone(UTC)
    except AttributeError, OverflowError, TypeError, ValueError:
        return False


def _selection_decision(
    candidate: NurtureInboundCandidate | NurtureCandidate | None,
    apply_requested: bool,
    draft: NurtureDraft | None,
    *,
    own_content_requested: bool = False,
    preview_only: bool = False,
) -> str:
    if candidate is None:
        return "NO_ACTION"
    if draft is not None:
        return "REPLY_REVIEW_ONLY" if preview_only else "REPLY_APPLY"
    if apply_requested and not own_content_requested:
        return "REPLY_RECOMMENDED_NO_DRAFT"
    if isinstance(candidate, NurtureInboundCandidate):
        return "INBOUND_CANDIDATE"
    return "OBSERVE_ONLY"


def _own_content_is_due(
    candidates: tuple[NurtureContentRecordV1, ...],
    preset: NurturePresetV1,
    now: datetime,
) -> bool:
    """Use the stricter of the preset cooldown and due interval since last publish."""

    published = tuple(item for item in candidates if item.publication_state == "PUBLISHED")
    if not published:
        return True
    last_action_at = max(
        (item.last_action_at for item in published if item.last_action_at is not None),
        default=None,
    )
    if last_action_at is None:
        return False
    try:
        elapsed = now - last_action_at.astimezone(UTC)
        required_seconds = max(
            preset.own_content_cooldown_seconds,
            preset.own_content_due_interval_seconds,
        )
        return elapsed >= timedelta(seconds=required_seconds)
    except AttributeError, OverflowError, TypeError, ValueError:
        return False


def _publishing_quota_exhausted(quota: object) -> bool:
    if type(quota) is not PublishingQuota:
        raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    usage = quota.usage
    total = quota.total
    for value in (usage, total):
        if value is not None and (type(value) is not int or value < 0):
            raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    return usage is not None and total is not None and usage >= total


def _discovery_state_for_bound_target(
    state: NurtureTargetStateV1,
    bound_target: NurtureTargetV1 | None,
    bound_fingerprint: str | None,
) -> NurtureTargetStateV1:
    if bound_fingerprint is None or bound_target is None or bound_target.action_state != "NONE":
        return state
    return NurtureTargetStateV1(
        version=state.version,
        account_id=state.account_id,
        preset_id=state.preset_id,
        targets=tuple(
            target for target in state.targets if target.fingerprint != bound_fingerprint
        ),
    )


def _mutation_target(
    candidate: NurtureInboundCandidate | NurtureCandidate,
) -> tuple[str, str | None]:
    if isinstance(candidate, NurtureInboundCandidate):
        if not candidate.root_thread_id or not candidate.reply_id:
            raise NurtureRunnerError("DISCOVERY_CONTRACT_MISMATCH")
        return candidate.root_thread_id, candidate.reply_id
    if not candidate.remote_thread_id:
        raise NurtureRunnerError("DISCOVERY_CONTRACT_MISMATCH")
    return candidate.remote_thread_id, None


def _reply_quota_exhausted(quota: object) -> bool:
    if type(quota) is not PublishingQuota:
        raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    usage = quota.reply_usage
    total = quota.reply_total
    for value in (usage, total):
        if value is not None and (type(value) is not int or value < 0):
            raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    return usage is not None and total is not None and usage >= total


def _surface_eligible(
    target: NurtureTargetV1 | None,
    cooldown_seconds: int,
    now: datetime,
) -> bool:
    if target is None:
        return True
    if target.action_state not in {"NONE", "CONFIRMED"}:
        return False
    try:
        last_seen_at = target.last_seen_at
        if type(last_seen_at) is not datetime or last_seen_at.tzinfo is None:
            return False
        if last_seen_at.utcoffset() is None:
            return False
        elapsed = now - last_seen_at.astimezone(UTC)
    except AttributeError, OverflowError, TypeError, ValueError:
        return False
    return elapsed >= timedelta(seconds=cooldown_seconds)


def _action_history_preserved(
    previous: NurtureTargetV1,
    observed: NurtureTargetV1,
) -> bool:
    return (
        observed.action_state == previous.action_state
        and observed.last_action_at == previous.last_action_at
        and observed.last_operation_id == previous.last_operation_id
    )


def _safe_stage(value: str, fallback: str) -> str:
    return value if type(value) is str and _SAFE_STAGE.fullmatch(value) else fallback


def _safe_code(value: object) -> str:
    return value if type(value) is str and value in _SAFE_RUNNER_CODES else "RUN_FAILED"


def _exception_code(error: Exception) -> object:
    try:
        return getattr(error, "code", None)
    except Exception:  # noqa: BLE001
        # A malformed error property must not prevent safe failure classification.
        return None


def _finish_failed(scope: NurtureRunScope, code: str, stage: str, run_id: UUID) -> None:
    try:
        scope.finish("FAILED", error_code=code, failed_stage=stage)
    except NurtureStateError as error:
        raise NurtureRunnerError(error.code, run_id) from None


def _best_effort_ambiguous(
    owner: NurtureAccountLock,
    scope: NurtureRunScope,
    preset: NurturePresetV1,
    fingerprint: str | None,
    run_id: UUID,
    operation_id: UUID,
    now: datetime,
) -> None:
    if fingerprint is not None:
        try:
            owner.complete_target_action(
                preset,
                fingerprint,
                run_id,
                "AMBIGUOUS",
                operation_id,
                now=now,
            )
        except Exception:  # noqa: BLE001, S110
            # Best-effort terminal repair must not hide the original ambiguous mutation outcome.
            pass
    try:
        scope.link_operation(operation_id)
    except Exception:  # noqa: BLE001, S110
        # Best-effort terminal repair must not hide the original ambiguous mutation outcome.
        pass
    try:
        scope.finish(
            "AMBIGUOUS",
            now=now,
            error_code="PUBLISH_OUTCOME_AMBIGUOUS",
            failed_stage="publish",
        )
    except Exception:  # noqa: BLE001, S110
        # Preserve the original ambiguous outcome if terminal receipt persistence also fails.
        pass


def _best_effort_content_ambiguous(
    owner: NurtureAccountLock,
    scope: NurtureRunScope,
    preset: NurturePresetV1,
    candidate: ContentCandidateV1,
    run_id: UUID,
    operation_id: UUID,
    now: datetime,
) -> None:
    try:
        owner.complete_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            run_id,
            "AMBIGUOUS",
            operation_id,
            now=now,
        )
    except Exception:  # noqa: BLE001, S110
        # Best-effort content-state repair must not hide the ambiguous mutation outcome.
        pass
    try:
        scope.link_operation(operation_id)
    except Exception:  # noqa: BLE001, S110
        # Best-effort journal linking must not permit a retry or mask the original failure.
        pass
    try:
        scope.update(
            decision_codes=(
                *scope.receipt.decision_codes[:-1],
                "CONTENT_PUBLISH_AMBIGUOUS",
            )
        )
    except Exception:  # noqa: BLE001, S110
        # Best-effort receipt update must not mask the original ambiguous outcome.
        pass
    try:
        scope.finish(
            "AMBIGUOUS",
            now=now,
            error_code="PUBLISH_OUTCOME_AMBIGUOUS",
            failed_stage="publish",
        )
    except Exception:  # noqa: BLE001, S110
        # Best-effort terminal receipt persistence must not hide the ambiguous outcome.
        pass
