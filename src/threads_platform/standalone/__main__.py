"""CLI for local account metadata and read-only Threads API commands."""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Any, Literal, NoReturn, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.browser.capabilities import (
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
from threads_platform.standalone.api import StandaloneApiError, bind_env_credential
from threads_platform.standalone.app import build_standalone_app
from threads_platform.standalone.manual_opportunities import (
    ManualOpportunityError,
    run_manual_list_cli,
    run_manual_review_cli,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeError
from threads_platform.standalone.manual_workflow import (
    ManualWorkflowError,
    run_manual_handoff_cli,
    run_manual_record_cli,
    run_manual_status_cli,
)
from threads_platform.standalone.mutations import (
    CarouselManifest,
    LocalOperationStore,
    StandaloneMutationError,
    load_carousel_manifest,
    validate_media_post_inputs,
    validate_reply_moderation_inputs,
)
from threads_platform.standalone.nurture import (
    NurturePresetError,
    NurturePresetV1,
    get_nurture_preset,
)
from threads_platform.standalone.nurture_content import (
    ContentCandidateV1,
    NurtureContentError,
    load_content_candidate,
)
from threads_platform.standalone.nurture_draft import (
    NurtureDraft,
    NurtureDraftError,
    load_nurture_draft,
)
from threads_platform.standalone.nurture_insights import (
    NurtureInsightsError,
    NurtureInsightsResult,
    NurtureInsightsService,
)
from threads_platform.standalone.nurture_quote_draft import (
    NurtureQuoteDraft,
    NurtureQuoteDraftError,
    load_nurture_quote_draft,
)
from threads_platform.standalone.nurture_runner import (
    NurtureRunner,
    NurtureRunnerError,
    NurtureRunnerInterrupted,
    NurtureRunResult,
)
from threads_platform.standalone.nurture_store import NurtureStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueueError,
    run_opportunity_import_cli,
    run_opportunity_list_cli,
    run_opportunity_remove_cli,
    run_opportunity_select_cli,
    run_opportunity_skip_cli,
)
from threads_platform.standalone.recruitment_content_cli import (
    RecruitmentContentCliError,
    run_recruitment_content_cli,
)
from threads_platform.standalone.recruitment_content_planner import RecruitmentContentPlannerError
from threads_platform.standalone.recruitment_qualification import (
    RecruitmentQualificationError,
    run_opportunity_rank_cli,
)
from threads_platform.standalone.recruitment_reply_assistant import (
    RecruitmentReplyAssistantError,
    run_opportunity_draft_cli,
)
from threads_platform.standalone.recruitment_reply_proposal import ReplyProposalError
from threads_platform.standalone.recurrences import (
    LocalRecurrenceRunner,
    LocalRecurrenceStore,
    RecurrenceRecord,
    StandaloneRecurrenceError,
)
from threads_platform.standalone.workflows import (
    FeedStep,
    LocalWorkflowRuntime,
    ProfileStep,
    StandaloneWorkflowError,
    ThreadStep,
    WorkflowPlan,
    WorkflowStepResult,
    is_read_only_workflow,
    load_workflow,
)


def _format_scalar(value: int | str | None) -> str:
    return "-" if value is None else str(value)


def _format_text(value: str | None) -> str:
    if value is None:
        return "-"
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:500] if normalized else "-"


def _format_bounded_text(value: str | None, limit: int) -> str:
    if value is None:
        return "-"
    safe_value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    normalized = re.sub(r"\s+", " ", safe_value).strip()
    return normalized[:limit] if normalized else "-"


def _format_https_url(value: str | None) -> str:
    if value is None or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return "-"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        parsed = None
        hostname = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "-"
    return value


def _format_discovery_cursor(value: str | None) -> str:
    if value is None or not value.strip() or len(value) > 4096 or not value.isprintable():
        return "-"
    return value


def _format_public_profile(profile: RemotePublicProfile) -> str:
    return (
        f"profile id={_format_bounded_text(profile.remote_author_id, 255)} "
        f"username={_format_bounded_text(profile.username, 255)} "
        f"name={_format_bounded_text(profile.display_name, 255)} "
        f"bio={_format_bounded_text(profile.biography, 500)} "
        f"picture={_format_https_url(profile.profile_picture_url)}\n"
    )


def _format_discovery_page(
    kind: str,
    page: DiscoveryPage,
    limit: int,
    *,
    workflow_step: int | None = None,
) -> str:
    threads = page.threads[: min(max(limit, 0), 50)]
    heading = (
        f"{kind} " if workflow_step is None else f"workflow step={workflow_step} action={kind} "
    )
    thread_heading = (
        "thread "
        if workflow_step is None
        else f"workflow step={workflow_step} action={kind} thread "
    )
    lines = [
        (
            f"{heading}count={len(threads)} "
            f"has_more={str(page.has_more).lower()} "
            f"next_cursor={_format_discovery_cursor(page.next_cursor)}\n"
        )
    ]
    for thread in threads:
        timestamp = thread.timestamp.isoformat() if thread.timestamp is not None else None
        quote = "-" if thread.is_quote_post is None else str(thread.is_quote_post).lower()
        has_replies = "-" if thread.has_replies is None else str(thread.has_replies).lower()
        lines.append(
            f"{thread_heading}{_format_bounded_text(thread.remote_thread_id, 255)} "
            f"username={_format_bounded_text(thread.username, 255)} "
            f"timestamp={_format_bounded_text(timestamp, 64)} "
            f"media_type={_format_bounded_text(thread.media_type, 80)} "
            f"quote={quote} has_replies={has_replies} "
            f"permalink={_format_https_url(thread.permalink)} "
            f"text={_format_bounded_text(thread.text, 500)}\n"
        )
    return "".join(lines)


async def _run_api_command(args: argparse.Namespace, root: Path, store: LocalAccountStore) -> str:
    async with build_standalone_app(root, store) as app:
        runtime = app.api
        if args.api_command == "quota":
            quota: PublishingQuota = await runtime.quota(args.alias)
            return (
                f"quota usage={_format_scalar(quota.usage)} "
                f"total={_format_scalar(quota.total)} "
                f"reply_usage={_format_scalar(quota.reply_usage)} "
                f"reply_total={_format_scalar(quota.reply_total)}\n"
            )
        if args.api_command == "media":
            media: RemoteMedia = await runtime.media(args.alias, args.media_id)
            return (
                f"media {media.media_id}\n"
                f"timestamp {_format_scalar(media.published_at)}\n"
                f"permalink {_format_scalar(media.permalink)}\n"
                f"text {_format_text(media.text)}\n"
            )
        if args.api_command == "post-insights":
            result = await runtime.post_insights(args.alias, args.media_id)
            values = " ".join(
                f"{metric.name.value}={_format_scalar(metric.value)}" for metric in result.metrics
            )
            return f"post-insights period={result.period} {values}\n"
        if args.api_command == "public-profile":
            profile = await runtime.public_profile(args.alias, args.username)
            return _format_public_profile(profile)
        if args.api_command == "profile-posts":
            discovery_page = await runtime.profile_posts(
                args.alias,
                args.username,
                after=args.after,
                limit=args.limit,
            )
            return _format_discovery_page("profile-posts", discovery_page, args.limit)
        if args.api_command == "search":
            discovery_page = await runtime.search(
                args.alias,
                args.query,
                search_mode=DiscoverySearchMode(args.mode.upper()),
                search_type=DiscoverySearchType(args.type.upper()),
                after=args.after,
                limit=args.limit,
            )
            return _format_discovery_page("search", discovery_page, args.limit)
        if args.api_command == "mentions":
            discovery_page = await runtime.mentions(args.alias, after=args.after, limit=args.limit)
            return _format_discovery_page("mentions", discovery_page, args.limit)

        if args.api_command == "replies":
            reply_page: ReplyPage = await runtime.replies(
                args.alias, args.thread_id, after=args.after
            )
            label = "replies"
        else:
            reply_page = await runtime.conversation(args.alias, args.thread_id, after=args.after)
            label = "conversation"
        lines = [
            (
                f"{label} count={len(reply_page.replies)} "
                f"has_more={str(reply_page.has_more).lower()} "
                f"next_cursor={_format_scalar(reply_page.next_cursor)}\n"
            )
        ]
        for reply in reply_page.replies:
            lines.append(
                f"reply {reply.reply_id} "
                f"timestamp={_format_scalar(reply.timestamp)} "
                f"root={_format_scalar(reply.root_post_id)} "
                f"parent={_format_scalar(reply.replied_to_id)} "
                f"text={_format_text(reply.text)}\n"
            )
        return "".join(lines)


async def _run_post_command(args: argparse.Namespace, root: Path, store: LocalAccountStore) -> str:
    async with build_standalone_app(root, store) as app:
        assert app.mutations is not None
        result = await app.mutations.publish_text(args.alias, args.text)
    return f"published operation={result.operation_id} media={result.media_id}\n"


async def _run_reply_command(args: argparse.Namespace, root: Path, store: LocalAccountStore) -> str:
    async with build_standalone_app(root, store) as app:
        assert app.mutations is not None
        result = await app.mutations.create_reply(
            args.alias,
            args.thread_id,
            args.text,
            parent_reply_id=args.parent_reply_id,
        )
    return f"replied operation={result.operation_id} reply={result.reply_id}\n"


async def _run_moderate_reply_command(
    alias: str, reply_id: str, action: str, root: Path, store: LocalAccountStore
) -> str:
    async with build_standalone_app(root, store) as app:
        assert app.mutations is not None
        result = await app.mutations.moderate_reply(alias, reply_id, action)
    return f"moderated operation={result.operation_id} action={result.action}\n"


async def _run_media_post_command(
    args: argparse.Namespace,
    root: Path,
    store: LocalAccountStore,
    *,
    media_type: Literal["IMAGE", "VIDEO"],
) -> str:
    media_url = args.image_url if media_type == "IMAGE" else args.video_url
    validate_media_post_inputs(media_url, args.text, args.alt_text)
    async with build_standalone_app(root, store) as app:
        assert app.mutations is not None
        if media_type == "IMAGE":
            result = await app.mutations.publish_image(
                args.alias, args.image_url, args.text, alt_text=args.alt_text
            )
        else:
            result = await app.mutations.publish_video(
                args.alias, args.video_url, args.text, alt_text=args.alt_text
            )
    return (
        f"published-{media_type.lower()} operation={result.operation_id} media={result.media_id}\n"
    )


async def _run_carousel_post_command(
    alias: str, manifest: CarouselManifest, root: Path, store: LocalAccountStore
) -> str:
    async with build_standalone_app(root, store) as app:
        assert app.mutations is not None
        result = await app.mutations.publish_carousel(alias, manifest)
    return f"published-carousel operation={result.operation_id} media={result.media_id}\n"


async def _run_workflow_command(
    plan: WorkflowPlan,
    root: Path,
    store: LocalAccountStore,
) -> str:
    results = await _execute_workflow_plan(plan, root, store)
    return _format_workflow_results(results)


async def _execute_workflow_plan(
    plan: WorkflowPlan,
    root: Path,
    store: LocalAccountStore,
) -> tuple[WorkflowStepResult, ...]:
    browser_steps = (FeedStep, ProfileStep, ThreadStep)
    needs_browser = any(isinstance(step, browser_steps) for step in plan.steps)
    needs_mutation = not is_read_only_workflow(plan)
    browser: Any = None
    if needs_browser:
        from threads_platform.standalone.runtime import LocalRuntime

        browser = LocalRuntime(root, store)
    async with build_standalone_app(root, store, include_mutations=needs_mutation) as app:
        runtime = LocalWorkflowRuntime(app.api, app.mutations, browser)
        return await runtime.run(plan)


async def _run_recurrence_command(
    recurrence_id: str,
    root: Path,
    accounts: LocalAccountStore,
    recurrences: LocalRecurrenceStore,
) -> None:
    async def execute_once(plan: WorkflowPlan) -> object:
        return await _execute_workflow_plan(plan, root, accounts)

    await LocalRecurrenceRunner(recurrences).run(recurrence_id, execute_once)


def _format_recurrence_record(record: RecurrenceRecord) -> str:
    last_outcome = record.last_outcome or "-"
    last_error = record.last_error_code or "-"
    last_step = _format_scalar(record.last_step_index)
    return (
        f"recurrence id={record.id} status={record.status} account={record.account} "
        f"every_seconds={record.interval_seconds} "
        f"next_due_at={_format_recurrence_timestamp(record.next_due_at)} "
        f"last_outcome={last_outcome} last_error_code={last_error} "
        f"last_step_index={last_step}\n"
    )


def _format_recurrence_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_workflow_results(results: tuple[WorkflowStepResult, ...]) -> str:
    lines: list[str] = []
    for result in results:
        value = result.value
        if isinstance(value, PublishingQuota):
            lines.append(
                f"workflow step={result.index} action=quota "
                f"usage={_format_scalar(value.usage)} total={_format_scalar(value.total)} "
                f"reply_usage={_format_scalar(value.reply_usage)} "
                f"reply_total={_format_scalar(value.reply_total)}\n"
            )
        elif isinstance(value, RemoteMedia):
            lines.append(
                f"workflow step={result.index} action=media id={value.media_id} "
                f"timestamp={_format_scalar(value.published_at)} "
                f"permalink={_format_scalar(value.permalink)} "
                f"text={_format_text(value.text)}\n"
            )
        elif isinstance(value, ReplyPage):
            lines.append(
                f"workflow step={result.index} action={result.action} "
                f"count={len(value.replies)} has_more={str(value.has_more).lower()} "
                f"next_cursor={_format_scalar(value.next_cursor)}\n"
            )
            for reply in value.replies:
                lines.append(
                    f"workflow step={result.index} action={result.action} reply {reply.reply_id} "
                    f"timestamp={_format_scalar(reply.timestamp)} "
                    f"root={_format_scalar(reply.root_post_id)} "
                    f"parent={_format_scalar(reply.replied_to_id)} "
                    f"text={_format_text(reply.text)}\n"
                )
        elif isinstance(value, BrowserTargetOpenResultV1):
            lines.append(
                f"workflow step={result.index} action={result.action} recognized "
                f"target={_format_bounded_text(value.target_ref, 255)}\n"
            )
        elif isinstance(value, BrowserFeedResultV1):
            lines.append(
                f"workflow step={result.index} action=feed count={len(value.observations)} "
                f"truncated={str(value.truncated).lower()}\n"
            )
            for item in value.observations:
                lines.append(
                    f"workflow step={result.index} action=feed item position={item.position} "
                    f"thread_ref={_format_bounded_text(item.thread_ref, 255)} "
                    f"author={_format_bounded_text(item.author_username, 30)} "
                    f"text={_format_bounded_text(item.text_excerpt, 500)}\n"
                )
        elif isinstance(value, RemotePublicProfile):
            lines.append(
                f"workflow step={result.index} action=public_profile {_format_public_profile(value)}"
            )
        elif isinstance(value, DiscoveryPage):
            lines.append(
                _format_discovery_page(
                    result.action,
                    value,
                    50,
                    workflow_step=result.index,
                )
            )
        else:
            assert result.action == "post_text"
            lines.append(
                f"workflow step={result.index} action=post_text published "
                f"operation={value.operation_id} media={value.media_id}\n"
            )
    lines.append(f"workflow completed steps={len(results)}\n")
    return "".join(lines)


async def _run_nurture_command(
    root: Path,
    accounts: LocalAccountStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    *,
    apply_requested: bool = False,
    draft: NurtureDraft | None = None,
    quote_draft: NurtureQuoteDraft | None = None,
    content_candidate: ContentCandidateV1 | None = None,
) -> NurtureRunResult:
    async with build_standalone_app(
        root,
        accounts,
        include_api=True,
        include_mutations=(
            draft is not None
            or quote_draft is not None
            or (content_candidate is not None and apply_requested)
        ),
        include_browser=False,
    ) as app:
        if draft is not None and app.mutations is None:
            raise NurtureRunnerError("RUN_FAILED")
        if quote_draft is not None and app.mutations is None:
            raise NurtureRunnerError("RUN_FAILED")
        return await NurtureRunner(app.api, NurtureStore(root)).run_with_selection(
            account,
            preset,
            apply_requested=apply_requested,
            draft=draft,
            quote_draft=quote_draft,
            content_candidate=content_candidate,
            mutations=app.mutations,
        )


def _format_nurture_run(
    account: LocalAccount,
    preset: NurturePresetV1,
    result: NurtureRunResult,
) -> str:
    run = result.receipt
    decision = run.decision_codes[0] if run.decision_codes else "NO_ACTION"
    target = f" target={result.target_fingerprint}" if result.target_fingerprint else ""
    candidate = (
        f" candidate={result.content_candidate_id}"
        if result.content_candidate_id is not None
        else ""
    )
    return (
        f"nurture run={run.id} account={account.alias} preset={preset.id} "
        f"outcome={run.outcome} discovered={run.discovered_count} "
        f"selected={run.selected_count} decision={decision} "
        f"inbound={result.inbound_status.value}{target}{candidate}\n"
    )


async def _run_nurture_insights_command(
    root: Path,
    accounts: LocalAccountStore,
    account: LocalAccount,
    preset: NurturePresetV1,
) -> NurtureInsightsResult:
    async with build_standalone_app(
        root,
        accounts,
        include_api=True,
        include_mutations=False,
        include_browser=False,
    ) as app:
        service = NurtureInsightsService(
            app.api,
            NurtureStore(root),
            LocalOperationStore(root),
        )
        return await service.refresh(account, preset)


def _format_nurture_insights(result: NurtureInsightsResult) -> str:
    return (
        f"nurture-insights refreshed={result.refreshed} "
        f"skipped_spacing={result.skipped_spacing} scoreable={result.scoreable} "
        f"insufficient={result.insufficient} below={result.below} "
        f"baseline={result.baseline} above={result.above} top={result.top}\n"
    )


def _handle_nurture_insights_cli_command(args: argparse.Namespace) -> int:
    try:
        preset = get_nurture_preset(args.preset)
    except NurturePresetError as error:
        raise NurtureInsightsError(error.code) from None
    root = resolve_standalone_data_root()
    accounts = LocalAccountStore(root)
    account = accounts.get(args.alias)
    result = asyncio.run(_run_nurture_insights_command(root, accounts, account, preset))
    sys.stdout.write(_format_nurture_insights(result))
    return 0


def _handle_nurture_command(args: argparse.Namespace) -> int:
    if args.nurture_command == "insights":
        return _handle_nurture_insights_cli_command(args)
    return _handle_nurture_cli_command(args)


def _run_nurture_cli(
    args: argparse.Namespace,
    root: Path,
    accounts: LocalAccountStore,
) -> str:
    try:
        preset = get_nurture_preset(args.preset)
    except NurturePresetError as error:
        raise NurtureRunnerError(error.code) from None
    account = accounts.get(args.alias)
    draft: NurtureDraft | None = None
    quote_draft: NurtureQuoteDraft | None = None
    content_candidate: ContentCandidateV1 | None = None
    if args.reply_file is not None:
        try:
            draft = load_nurture_draft(Path(args.reply_file))
        except NurtureDraftError as error:
            raise NurtureRunnerError(error.code) from None
    if args.post_file is not None:
        try:
            content_candidate = load_content_candidate(
                Path(args.post_file), account_alias=account.alias, preset_id=preset.id
            )
        except NurtureContentError as error:
            raise NurtureRunnerError(error.code) from None
    if args.quote_file is not None:
        try:
            quote_draft = load_nurture_quote_draft(Path(args.quote_file))
        except NurtureQuoteDraftError as error:
            raise NurtureRunnerError(error.code) from None
    result = asyncio.run(
        _run_nurture_command(
            root,
            accounts,
            account,
            preset,
            apply_requested=args.apply,
            draft=draft,
            quote_draft=quote_draft,
            content_candidate=content_candidate,
        )
    )
    return _format_nurture_run(account, preset, result)


def _handle_nurture_cli_command(args: argparse.Namespace) -> int:
    supplied_draft_files = sum(
        value is not None for value in (args.reply_file, args.post_file, args.quote_file)
    )
    if supplied_draft_files > 1:
        raise NurtureRunnerError("NURTURE_INPUT_CONFLICT")
    if args.reply_file is not None and not args.apply:
        raise NurtureRunnerError("INVALID_NURTURE_DRAFT")
    if args.quote_file is not None and not args.apply:
        raise NurtureRunnerError("INVALID_NURTURE_QUOTE_DRAFT")
    root = resolve_standalone_data_root()
    accounts = LocalAccountStore(root)
    sys.stdout.write(_run_nurture_cli(args, root, accounts))
    return 0


class _BrowserNurtureRunResultView(Protocol):
    @property
    def status(self) -> StrEnum: ...

    @property
    def reason_code(self) -> str | None: ...

    @property
    def cycles(self) -> int: ...

    @property
    def feed_candidates(self) -> int: ...

    @property
    def scanned(self) -> int: ...

    @property
    def relevant(self) -> int: ...

    @property
    def irrelevant(self) -> int: ...

    @property
    def unknown(self) -> int: ...

    @property
    def duplicates(self) -> int: ...

    @property
    def scrolls(self) -> int: ...

    @property
    def sessions_opened(self) -> int: ...

    @property
    def checkpoint_cycles(self) -> int: ...


def _format_browser_nurture_result(result: _BrowserNurtureRunResultView, *, source: str) -> str:
    status = result.status.value
    reason = result.reason_code or "-"
    return (
        f"BROWSER_NURTURE status={status} source={source} reason={reason} "
        f"cycles={result.cycles} "
        f"search_candidates={result.feed_candidates} "
        f"search_scanned={result.scanned} "
        f"search_relevant={result.relevant} "
        f"search_irrelevant={result.irrelevant} "
        f"search_unknown={result.unknown} "
        f"search_duplicates={result.duplicates} "
        f"search_scrolls={result.scrolls} "
        f"sessions_opened={result.sessions_opened} "
        f"checkpoint_cycles={result.checkpoint_cycles}\n"
    )


def _handle_browser_ui_diagnostic(args: argparse.Namespace) -> int:
    from threads_platform.standalone.browser_nurture import BrowserNurtureError
    from threads_platform.standalone.browser_nurture_state import (
        BrowserNurtureStateError,
        BrowserNurtureStateStore,
    )
    from threads_platform.standalone.browser_ui_diagnostic import (
        run_browser_ui_diagnostic,
    )
    from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

    try:
        if not args.confirm_local_read_only:
            raise BrowserNurtureError("LOCAL_READ_CONFIRMATION_REQUIRED")
        if not _browser_nurture_interactive_console():
            raise BrowserNurtureError("INTERACTIVE_CONSOLE_REQUIRED")
        data_root = resolve_standalone_data_root()
        accounts = LocalAccountStore(data_root)
        account = accounts.get(args.alias)
        if (
            re.fullmatch(
                r"(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})",
                account.alias.casefold(),
            )
            is not None
        ):
            raise BrowserNurtureError("LIVE_ACCOUNT_REQUIRED")
        state = BrowserNurtureStateStore(data_root)
        config = state.load_config(account.id)
        if config is None:
            raise BrowserNurtureError("CONFIG_NOT_FOUND")
        report = asyncio.run(
            run_browser_ui_diagnostic(
                LocalRuntime(data_root, accounts),
                account.alias,
                config,
                output=sys.stdout,
            )
        )
    except (
        BrowserNurtureError,
        BrowserNurtureStateError,
        StandaloneRuntimeError,
        StandaloneAccountError,
    ) as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except KeyboardInterrupt:
        sys.stdout.write(
            '{"schema":"THREADS_UI_DIAGNOSTIC_V2","status":"STOPPED",'
            '"reason_code":"INTERRUPTED","stages":{'
            '"home":{"status":"NOT_OBSERVED"},'
            '"search":{"status":"NOT_OBSERVED"},'
            '"detail":{"status":"NOT_OBSERVED"}}}\n'
        )
        return 0
    except Exception:  # noqa: BLE001 - fixed CLI boundary for private browser data.
        sys.stderr.write("ERROR DIAGNOSTIC_FAILED\n")
        return 1
    return 0 if report["status"] in {"COMPLETE", "STOPPED"} else 1


def _handle_browser_nurture_command(args: argparse.Namespace) -> int:
    if args.browser_nurture_command == "inspect-ui":
        return _handle_browser_ui_diagnostic(args)

    from threads_platform.standalone.browser_nurture import (
        BrowserNurtureError,
        BrowserNurtureRunner,
        BrowserNurtureSource,
        load_synthetic_nurture_fixture,
        validate_synthetic_nurture_root,
    )
    from threads_platform.standalone.browser_nurture_state import (
        BrowserNurtureStateError,
        BrowserNurtureStateStore,
        browser_nurture_policy_fingerprint,
        validate_browser_nurture_config,
    )
    from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

    if args.browser_nurture_command == "configure":
        try:
            root = resolve_standalone_data_root()
            accounts = LocalAccountStore(root)
            account = accounts.get(args.alias)
            config = validate_browser_nurture_config(
                {
                    "schema_version": 1,
                    "account_uuid": str(account.id),
                    "expected_username": args.expected_username,
                    "topics": args.topic,
                    "relevance_terms": args.relevance_term or args.topic,
                    "max_posts_per_cycle": args.max_posts_per_cycle,
                    "idle_seconds": args.idle_seconds,
                }
            )
            state = BrowserNurtureStateStore(root)
            with LocalRuntime(root, accounts).account_lock(account):
                state.save_config(config)
        except (
            BrowserNurtureError,
            BrowserNurtureStateError,
            StandaloneRuntimeError,
            StandaloneAccountError,
        ) as error:
            sys.stderr.write(f"ERROR {error.code}\n")
            return 1
        sys.stdout.write(
            "BROWSER_NURTURE_CONFIG status=SAVED "
            f"topics={len(config.topics)} relevance_terms={len(config.relevance_terms)} "
            f"max_posts_per_cycle={config.max_posts_per_cycle} "
            f"idle_seconds={config.idle_seconds}\n"
        )
        return 0

    if args.browser_nurture_command == "enable-continuous":
        try:
            if not args.confirm_supervised_read_only:
                raise BrowserNurtureError("SUPERVISED_READ_CONFIRMATION_REQUIRED")
            if not _browser_nurture_interactive_console():
                raise BrowserNurtureError("INTERACTIVE_CONSOLE_REQUIRED")
            root = resolve_standalone_data_root()
            accounts = LocalAccountStore(root)
            account = accounts.get(args.alias)
            state = BrowserNurtureStateStore(root)
            with LocalRuntime(root, accounts).account_lock(account):
                if (
                    re.fullmatch(r"(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})", account.alias)
                    is not None
                ):
                    raise BrowserNurtureError("LIVE_ACCOUNT_REQUIRED")
                config = state.load_config(account.id)
                if config is None:
                    raise BrowserNurtureError("CONFIG_NOT_FOUND")
                policy_fingerprint = browser_nurture_policy_fingerprint(config)
                checkpoint = state.load_checkpoint(account.id)
                if (
                    checkpoint.completed_cycles < 1
                    or checkpoint.policy_fingerprint != policy_fingerprint
                ):
                    raise BrowserNurtureError("SUPERVISED_CYCLE_REQUIRED")
                state.set_continuous_opt_in(
                    account.id,
                    policy_fingerprint=policy_fingerprint,
                    enabled=True,
                )
        except (
            BrowserNurtureError,
            BrowserNurtureStateError,
            StandaloneRuntimeError,
            StandaloneAccountError,
        ) as error:
            sys.stderr.write(f"ERROR {error.code}\n")
            return 1
        sys.stdout.write("BROWSER_NURTURE_CONTINUOUS status=ENABLED\n")
        return 0

    if args.browser_nurture_command == "disable-continuous":
        try:
            root = resolve_standalone_data_root()
            accounts = LocalAccountStore(root)
            account = accounts.get(args.alias)
            state = BrowserNurtureStateStore(root)
            with LocalRuntime(root, accounts).account_lock(account):
                state.disable_continuous_opt_in(account.id)
        except (
            BrowserNurtureStateError,
            StandaloneRuntimeError,
            StandaloneAccountError,
        ) as error:
            sys.stderr.write(f"ERROR {error.code}\n")
            return 1
        sys.stdout.write("BROWSER_NURTURE_CONTINUOUS status=DISABLED\n")
        return 0

    try:
        if args.source == "browser":
            if not args.continuous and args.max_cycles not in (None, 1):
                raise BrowserNurtureError("RUN_POLICY_INVALID")
            if args.fixture_file is not None or args.data_root is not None:
                raise BrowserNurtureError("LIVE_DATA_ROOT_NOT_ALLOWED")
            if args.continuous:
                if args.confirm_live_read_only:
                    raise BrowserNurtureError("RUN_POLICY_INVALID")
            else:
                if not args.confirm_live_read_only:
                    raise BrowserNurtureError("LIVE_CONFIRMATION_REQUIRED")
                if not _browser_nurture_interactive_console():
                    raise BrowserNurtureError("INTERACTIVE_CONSOLE_REQUIRED")
            data_root = resolve_standalone_data_root()
            accounts = LocalAccountStore(data_root)
            account = accounts.get(args.alias)
            if (
                re.fullmatch(r"(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})", account.alias)
                is not None
            ):
                raise BrowserNurtureError("LIVE_ACCOUNT_REQUIRED")
            state = BrowserNurtureStateStore(data_root)
            cycles = args.max_cycles
            if cycles is None and not args.continuous:
                cycles = 1
            result = _run_browser_nurture_with_stop(
                BrowserNurtureRunner(LocalRuntime(data_root, accounts), state),
                account.alias,
                source=BrowserNurtureSource.BROWSER_UI,
                max_cycles=cycles,
                continuous=args.continuous,
                idle_seconds=args.idle_seconds,
            )
            output_source = "BROWSER_UI"
        else:
            if args.confirm_live_read_only:
                raise BrowserNurtureError("RUN_POLICY_INVALID")
            if args.source != "ui-fixture":
                raise BrowserNurtureError("SOURCE_INVALID")
            if not isinstance(args.data_root, str) or not args.data_root.strip():
                raise BrowserNurtureError("DATA_ROOT_REQUIRED")
            if not isinstance(args.fixture_file, str) or not args.fixture_file.strip():
                raise BrowserNurtureError("FIXTURE_REQUIRED")
            data_root_candidate = Path(args.data_root).expanduser()
            if not data_root_candidate.is_absolute():
                raise BrowserNurtureError("DATA_ROOT_INVALID")
            data_root = validate_synthetic_nurture_root(data_root_candidate)
            accounts = LocalAccountStore(data_root)
            account = accounts.get(args.alias)
            if (
                re.fullmatch(r"(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})", account.alias)
                is None
            ):
                raise BrowserNurtureError("SYNTHETIC_ACCOUNT_REQUIRED")
            state = BrowserNurtureStateStore(data_root)
            config = state.load_config(account.id)
            if config is None:
                raise BrowserNurtureError("CONFIG_NOT_FOUND")
            if (
                re.fullmatch(
                    r"(?:test|synthetic|fixture)[._-][A-Za-z0-9._-]{1,28}",
                    config.expected_username,
                )
                is None
            ):
                raise BrowserNurtureError("SYNTHETIC_ACTOR_REQUIRED")
            fixture = load_synthetic_nurture_fixture(Path(args.fixture_file), config)
            cycles = args.max_cycles
            if cycles is None and not args.continuous:
                cycles = 1
            result = _run_browser_nurture_with_stop(
                BrowserNurtureRunner(LocalRuntime(data_root, accounts), state),
                account.alias,
                fixture,
                source=BrowserNurtureSource.UI_FIXTURE,
                max_cycles=cycles,
                continuous=args.continuous,
                idle_seconds=args.idle_seconds,
            )
            output_source = "SYNTHETIC_FIXTURE"
    except (
        BrowserNurtureError,
        BrowserNurtureStateError,
        StandaloneRuntimeError,
        StandaloneAccountError,
    ) as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except KeyboardInterrupt:
        output_source = "BROWSER_UI" if args.source == "browser" else "SYNTHETIC_FIXTURE"
        sys.stdout.write(
            f"BROWSER_NURTURE status=STOPPED source={output_source} "
            "reason=INTERRUPTED cycles=0 search_candidates=0 search_scanned=0 "
            "search_relevant=0 search_irrelevant=0 search_unknown=0 "
            "search_duplicates=0 search_scrolls=0 sessions_opened=0 "
            "checkpoint_cycles=preserved\n"
        )
        return 130
    except Exception:  # noqa: BLE001 - fixed safe CLI boundary for fixture failures.
        sys.stderr.write("ERROR BROWSER_NURTURE_FAILED\n")
        return 1
    sys.stdout.write(_format_browser_nurture_result(result, source=output_source))
    return 0 if result.status.value == "COMPLETE" else 1


def _browser_nurture_interactive_console() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_browser_nurture_with_stop(
    runner: Any,
    alias: str,
    fixture: Any = None,
    *,
    source: Any,
    max_cycles: int | None,
    continuous: bool,
    idle_seconds: int | None,
) -> Any:
    async def run() -> Any:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        previous_handler = signal.getsignal(signal.SIGINT)

        def request_stop(_signal_number: int, _frame: FrameType | None) -> None:
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, request_stop)
        try:
            return await runner.run(
                alias,
                fixture,
                source=source,
                max_cycles=max_cycles,
                continuous=continuous,
                idle_seconds=idle_seconds,
                stop_event=stop_event,
            )
        finally:
            signal.signal(signal.SIGINT, previous_handler)

    return asyncio.run(run())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-local")
    commands = parser.add_subparsers(dest="command", required=True)

    account_parser = commands.add_parser("account")
    account_commands = account_parser.add_subparsers(dest="account_command", required=True)
    add_parser = account_commands.add_parser("add")
    add_parser.add_argument("alias")
    account_commands.add_parser("list")
    login_parser = account_commands.add_parser("login")
    login_parser.add_argument("alias")
    credential_parser = account_commands.add_parser("credential")
    credential_commands = credential_parser.add_subparsers(dest="credential_command", required=True)
    set_env_parser = credential_commands.add_parser("set-env")
    set_env_parser.add_argument("alias")
    set_env_parser.add_argument("variable_name")

    profile_parser = commands.add_parser("profile")
    profile_parser.add_argument("alias")
    profile_parser.add_argument("username")
    thread_parser = commands.add_parser("thread")
    thread_parser.add_argument("alias")
    thread_parser.add_argument("thread_ref")
    feed_parser = commands.add_parser("feed")
    feed_parser.add_argument("alias")
    feed_parser.add_argument("--limit", type=int, default=10)

    browser_parser = commands.add_parser("browser")
    browser_commands = browser_parser.add_subparsers(dest="browser_command", required=True)
    open_thread_parser = browser_commands.add_parser("open-thread")
    open_thread_parser.add_argument("alias")
    open_thread_parser.add_argument("thread_ref")
    open_thread_parser.add_argument("--interactive", action="store_true", required=True)

    api_parser = commands.add_parser("api")
    api_commands = api_parser.add_subparsers(dest="api_command", required=True)
    quota_parser = api_commands.add_parser("quota")
    quota_parser.add_argument("alias")
    media_parser = api_commands.add_parser("media")
    media_parser.add_argument("alias")
    media_parser.add_argument("media_id")
    insights_parser = api_commands.add_parser("post-insights")
    insights_parser.add_argument("alias")
    insights_parser.add_argument("media_id")
    profile_parser = api_commands.add_parser("public-profile")
    profile_parser.add_argument("alias")
    profile_parser.add_argument("username")
    for name in ("profile-posts", "search", "mentions"):
        page_parser = api_commands.add_parser(name)
        page_parser.add_argument("alias")
        if name == "profile-posts":
            page_parser.add_argument("username")
        elif name == "search":
            page_parser.add_argument("query")
            page_parser.add_argument("--mode", choices=("keyword", "tag"), required=True)
            page_parser.add_argument("--type", choices=("top", "recent"), required=True)
        page_parser.add_argument("--after")
        page_parser.add_argument("--limit", type=int, default=25)
    for name in ("replies", "conversation"):
        page_parser = api_commands.add_parser(name)
        page_parser.add_argument("alias")
        page_parser.add_argument("thread_id")
        page_parser.add_argument("--after")

    post_parser = commands.add_parser("post")
    post_parser.add_argument("alias")
    post_parser.add_argument("text")
    reply_parser = commands.add_parser("reply")
    reply_parser.add_argument("alias")
    reply_parser.add_argument("thread_id")
    reply_parser.add_argument("text")
    reply_parser.add_argument("--parent-reply-id")
    moderation_parser = commands.add_parser("moderate-reply")
    moderation_parser.add_argument("alias")
    moderation_parser.add_argument("reply_id")
    moderation_parser.add_argument("action")
    image_parser = commands.add_parser("post-image")
    image_parser.add_argument("alias")
    image_parser.add_argument("image_url")
    image_parser.add_argument("text", nargs="?")
    image_parser.add_argument("--alt-text")
    video_parser = commands.add_parser("post-video")
    video_parser.add_argument("alias")
    video_parser.add_argument("video_url")
    video_parser.add_argument("text", nargs="?")
    video_parser.add_argument("--alt-text")
    carousel_parser = commands.add_parser("post-carousel")
    carousel_parser.add_argument("alias")
    carousel_parser.add_argument("manifest")
    operation_parser = commands.add_parser("operation")
    operation_commands = operation_parser.add_subparsers(dest="operation_command", required=True)
    show_parser = operation_commands.add_parser("show")
    show_parser.add_argument("operation_id")

    workflow_parser = commands.add_parser("workflow")
    workflow_commands = workflow_parser.add_subparsers(dest="workflow_command", required=True)
    run_workflow_parser = workflow_commands.add_parser("run")
    run_workflow_parser.add_argument("file")

    recurrence_parser = commands.add_parser("recurrence")
    recurrence_commands = recurrence_parser.add_subparsers(dest="recurrence_command", required=True)
    create_recurrence_parser = recurrence_commands.add_parser("create")
    create_recurrence_parser.add_argument("file")
    create_recurrence_parser.add_argument("--every-seconds", type=int, required=True)
    recurrence_commands.add_parser("list")
    show_recurrence_parser = recurrence_commands.add_parser("show")
    show_recurrence_parser.add_argument("id")
    run_recurrence_parser = recurrence_commands.add_parser("run")
    run_recurrence_parser.add_argument("id")
    disable_recurrence_parser = recurrence_commands.add_parser("disable")
    disable_recurrence_parser.add_argument("id")

    nurture_parser = commands.add_parser("nurture")
    nurture_commands = nurture_parser.add_subparsers(dest="nurture_command", required=True)
    run_nurture_parser = nurture_commands.add_parser("run")
    run_nurture_parser.add_argument("alias")
    run_nurture_parser.add_argument("--preset", required=True)
    run_nurture_parser.add_argument("--apply", action="store_true")
    run_nurture_parser.add_argument("--reply-file")
    run_nurture_parser.add_argument("--post-file")
    run_nurture_parser.add_argument("--quote-file")
    insights_nurture_parser = nurture_commands.add_parser("insights")
    nurture_insights_commands = insights_nurture_parser.add_subparsers(
        dest="nurture_insights_command", required=True
    )
    refresh_insights_parser = nurture_insights_commands.add_parser("refresh")
    refresh_insights_parser.add_argument("alias")
    refresh_insights_parser.add_argument("--preset", required=True)

    browser_nurture_parser = commands.add_parser(
        "browser-nurture",
        help="configure and run account-scoped read-only Browser Nurture",
    )
    browser_nurture_parser.error = lambda message: _redacted_parser_error(
        browser_nurture_parser,
        message,
    )
    browser_nurture_commands = browser_nurture_parser.add_subparsers(
        dest="browser_nurture_command",
        required=True,
    )
    configure_browser_nurture = browser_nurture_commands.add_parser(
        "configure",
        help="store private topic, relevance and expected-account settings",
    )
    configure_browser_nurture.add_argument("alias")
    configure_browser_nurture.add_argument("--expected-username", required=True)
    configure_browser_nurture.add_argument("--topic", action="append", required=True)
    configure_browser_nurture.add_argument("--relevance-term", action="append")
    configure_browser_nurture.add_argument("--max-posts-per-cycle", type=int, default=20)
    configure_browser_nurture.add_argument("--idle-seconds", type=int, default=900)
    enable_browser_nurture_continuous = browser_nurture_commands.add_parser(
        "enable-continuous",
        help="enable headed continuous runs after a supervised live cycle",
    )
    enable_browser_nurture_continuous.add_argument("alias")
    enable_browser_nurture_continuous.add_argument(
        "--confirm-supervised-read-only",
        action="store_true",
        help="confirm the supervised, read-only cycle was reviewed on screen",
    )
    disable_browser_nurture_continuous = browser_nurture_commands.add_parser(
        "disable-continuous",
        help="disable the account-scoped continuous-run opt-in",
    )
    disable_browser_nurture_continuous.add_argument("alias")
    inspect_browser_ui = browser_nurture_commands.add_parser(
        "inspect-ui",
        help="inspect Home, Search and one Detail page with a private read-only report",
    )
    inspect_browser_ui.add_argument("alias")
    inspect_browser_ui.add_argument(
        "--confirm-local-read-only",
        action="store_true",
        help="confirm this supervised session only observes the current page",
    )
    run_browser_nurture = browser_nurture_commands.add_parser(
        "run",
        help="run one bounded cycle or remain in the foreground until stopped",
    )
    run_browser_nurture.add_argument("alias")
    run_browser_nurture.add_argument(
        "--source",
        choices=("browser", "ui-fixture"),
        required=True,
        help="browser is live headed UI; ui-fixture is local synthetic Chromium only",
    )
    run_browser_nurture.add_argument("--fixture-file")
    run_browser_nurture.add_argument("--data-root")
    run_browser_nurture.add_argument("--continuous", action="store_true")
    run_browser_nurture.add_argument(
        "--confirm-live-read-only",
        action="store_true",
        help="confirm this interactive one-shot may read the selected account UI",
    )
    run_browser_nurture.add_argument("--max-cycles", type=int)
    run_browser_nurture.add_argument("--idle-seconds", type=int)

    recruitment_parser = commands.add_parser("recruitment")
    recruitment_commands = recruitment_parser.add_subparsers(
        dest="recruitment_command", required=True
    )
    browser_watch_parser = recruitment_commands.add_parser(
        "browser-watch",
        help="run a finite synthetic-only foreground Browser Jobs watch",
    )
    browser_watch_parser.error = lambda message: _redacted_parser_error(
        browser_watch_parser, message
    )
    browser_watch_parser.add_argument(
        "--source",
        choices=("fixture", "dom-fixture", "ui-fixture", "browser", "manual"),
        required=True,
        help=(
            "fixture maps synthetic JSON to BJ-02; dom-fixture renders local synthetic HTML; "
            "ui-fixture runs the B1 semantic Chromium reader on local synthetic HTML; "
            "browser is disabled pending verified operator permission; "
            "manual returns UNKNOWN / NO_TEXT"
        ),
    )
    browser_watch_parser.add_argument(
        "--account",
        help="synthetic alias beginning with test- or synthetic- (fixture sources only)",
    )
    browser_watch_parser.add_argument(
        "--data-root",
        help="caller-created empty private synthetic directory outside the checkout",
    )
    browser_watch_parser.add_argument(
        "--fixture-file",
        help="bounded private UTF-8 synthetic manifest outside the checkout",
    )
    browser_watch_parser.add_argument("--run-id", help="stable local synthetic run ID")
    browser_watch_parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="finite cycle cap from 1 to 5 (default: 1)",
    )
    browser_watch_parser.add_argument(
        "--max-posts-per-cycle",
        type=int,
        default=20,
        help="fixture post cap from 1 to 20 (default: 20)",
    )
    browser_watch_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="foreground interval; at least 15 minutes for multiple cycles",
    )

    manual_review_parser = recruitment_commands.add_parser("manual-review")
    manual_review_parser.add_argument("alias")
    manual_review_parser.add_argument("--targets-file", required=True)
    manual_review_parser.add_argument("--select", type=int, required=True)
    manual_review_parser.add_argument("--draft-file", required=True)
    manual_review_parser.add_argument("--context-file")
    manual_review_parser.add_argument("--confirm-target")
    manual_list_parser = recruitment_commands.add_parser("manual-list")
    manual_list_parser.add_argument("alias")
    manual_list_parser.add_argument("--targets-file", required=True)
    manual_handoff_parser = recruitment_commands.add_parser("manual-handoff")
    manual_handoff_parser.add_argument("alias")
    manual_handoff_parser.add_argument("--targets-file", required=True)
    manual_handoff_parser.add_argument("--select", type=int, required=True)
    manual_handoff_parser.add_argument("--confirm-target", required=True)
    manual_handoff_parser.add_argument("--draft-file", required=True)
    manual_handoff_parser.add_argument("--approve-sha", required=True)
    manual_handoff_parser.add_argument("--context-file")
    manual_handoff_parser.add_argument("--interactive", action="store_true", required=True)
    manual_record_parser = recruitment_commands.add_parser("manual-record")
    manual_record_parser.add_argument("alias")
    manual_record_parser.add_argument("--targets-file", required=True)
    manual_record_parser.add_argument("--select", type=int, required=True)
    manual_record_parser.add_argument("--confirm-target", required=True)
    manual_record_parser.add_argument("--draft-file", required=True)
    manual_record_parser.add_argument("--approve-sha", required=True)
    manual_record_parser.add_argument("--attempt-id", required=True)
    manual_record_parser.add_argument(
        "--outcome",
        choices=("ABORTED_NOT_SENT", "OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"),
        required=True,
    )
    manual_record_parser.add_argument("--context-file")
    manual_status_parser = recruitment_commands.add_parser("manual-status")
    manual_status_parser.add_argument("alias")
    manual_status_parser.add_argument("--targets-file", required=True)
    manual_status_parser.add_argument("--select", type=int, required=True)
    manual_status_parser.add_argument("--confirm-target", required=True)

    opportunity_import_parser = recruitment_commands.add_parser("opportunity-import")
    opportunity_import_parser.add_argument("alias")
    opportunity_import_parser.add_argument("--targets-file", required=True)
    opportunity_import_parser.add_argument("--context-file")
    opportunity_import_parser.add_argument(
        "--context-completeness", choices=("UNKNOWN", "PARTIAL", "COMPLETE")
    )
    opportunity_list_parser = recruitment_commands.add_parser("opportunity-list")
    opportunity_list_parser.add_argument("alias")
    opportunity_select_parser = recruitment_commands.add_parser("opportunity-select")
    opportunity_select_parser.add_argument("alias")
    opportunity_select_parser.add_argument("--id", required=True)
    opportunity_select_parser.add_argument("--confirm-target")
    opportunity_select_parser.add_argument("--context-file")
    opportunity_select_parser.add_argument(
        "--context-completeness", choices=("UNKNOWN", "PARTIAL", "COMPLETE")
    )
    opportunity_select_parser.add_argument("--draft-file")
    opportunity_skip_parser = recruitment_commands.add_parser("opportunity-skip")
    opportunity_skip_parser.add_argument("alias")
    opportunity_skip_parser.add_argument("--id", required=True)
    opportunity_skip_parser.add_argument("--reason-file")
    opportunity_remove_parser = recruitment_commands.add_parser("opportunity-remove")
    opportunity_remove_parser.add_argument("alias")
    opportunity_remove_parser.add_argument("--id", required=True)
    opportunity_rank_parser = recruitment_commands.add_parser("opportunity-rank")
    opportunity_rank_parser.add_argument("alias")
    opportunity_rank_parser.add_argument("--limit", type=int, default=10)
    opportunity_draft_parser = recruitment_commands.add_parser("opportunity-draft")
    opportunity_draft_parser.add_argument("alias")
    opportunity_draft_parser.add_argument("--id", required=True)
    opportunity_draft_parser.add_argument("--facts-file")

    content_add_parser = recruitment_commands.add_parser("content-add")
    content_add_parser.add_argument("alias")
    content_add_parser.add_argument("--text-file", required=True)
    content_add_parser.add_argument("--scheduled-utc", required=True)
    content_add_parser.add_argument("--assert-rights", action="store_true")
    content_add_parser.add_argument("--assert-consent", action="store_true")
    for content_command in (
        "content-list",
        "content-due",
        "content-upcoming",
        "content-calendar-export",
    ):
        recruitment_commands.add_parser(content_command).add_argument("alias")
    for content_command in ("content-show", "content-approve", "content-reject"):
        content_parser = recruitment_commands.add_parser(content_command)
        content_parser.add_argument("alias")
        content_parser.add_argument("--id", required=True)
    return parser


def _redacted_parser_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    del message
    parser.exit(2, "ERROR CLI_ARGUMENT_INVALID\n")


def _format_feed_result(result: Any) -> str:
    lines = [f"feed count={len(result.observations)} truncated={str(result.truncated).lower()}\n"]
    for item in result.observations:
        lines.append(
            f"item position={item.position} "
            f"thread_ref={_format_bounded_text(item.thread_ref, 255)} "
            f"author={_format_bounded_text(item.author_username, 30)} "
            f"text={_format_bounded_text(item.text_excerpt, 500)}\n"
        )
    return "".join(lines)


def _run_browser_command(args: argparse.Namespace, root: Path, store: LocalAccountStore) -> int:
    """Load the browser stack only for an explicitly selected local browser command."""
    from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

    runtime = LocalRuntime(root, store)
    try:
        if args.command == "browser":
            result = asyncio.run(runtime.open_thread_interactive(args.alias, args.thread_ref))
            sys.stdout.write(
                f"browser session closed target={result.target_ref} identity=UNKNOWN_IDENTITY\n"
            )
            return 0
        if args.command == "account":
            asyncio.run(runtime.login(args.alias))
            sys.stdout.write("login browser closed authentication=NOT_VERIFIED\n")
            return 0
        if args.command == "profile":
            result = asyncio.run(runtime.open_profile(args.alias, args.username))
            sys.stdout.write(f"profile recognized target={result.target_ref}\n")
            return 0
        if args.command == "thread":
            result = asyncio.run(runtime.open_thread(args.alias, args.thread_ref))
            sys.stdout.write(f"thread recognized target={result.target_ref}\n")
            return 0
        result = asyncio.run(runtime.browse_feed(args.alias, args.limit))
        sys.stdout.write(_format_feed_result(result))
        return 0
    except StandaloneRuntimeError as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1


def _handle_recruitment_command(args: argparse.Namespace) -> int:
    if args.recruitment_command == "browser-watch":
        from threads_platform.standalone.recruitment_browser_jobs_cli import (
            run_recruitment_browser_jobs_cli,
        )

        return run_recruitment_browser_jobs_cli(args)
    if args.recruitment_command.startswith("content-"):
        return run_recruitment_content_cli(
            args.recruitment_command,
            args.alias,
            packet_id=getattr(args, "id", None),
            text_file=None if getattr(args, "text_file", None) is None else Path(args.text_file),
            scheduled_utc=getattr(args, "scheduled_utc", None),
            rights_asserted=getattr(args, "assert_rights", False),
            consent_asserted=getattr(args, "assert_consent", False),
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-draft":
        return run_opportunity_draft_cli(
            args.alias,
            args.id,
            facts_file=None if args.facts_file is None else Path(args.facts_file),
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-rank":
        return run_opportunity_rank_cli(
            args.alias,
            limit=args.limit,
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-import":
        return run_opportunity_import_cli(
            args.alias,
            Path(args.targets_file),
            None if args.context_file is None else Path(args.context_file),
            args.context_completeness,
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-list":
        return run_opportunity_list_cli(
            args.alias,
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-select":
        return run_opportunity_select_cli(
            args.alias,
            args.id,
            data_root_factory=resolve_standalone_data_root,
            targets_confirmed=args.confirm_target,
            draft_file=None if args.draft_file is None else Path(args.draft_file),
            context_file=None if args.context_file is None else Path(args.context_file),
            context_completeness=args.context_completeness,
        )
    if args.recruitment_command == "opportunity-skip":
        return run_opportunity_skip_cli(
            args.alias,
            args.id,
            None if args.reason_file is None else Path(args.reason_file),
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "opportunity-remove":
        return run_opportunity_remove_cli(
            args.alias,
            args.id,
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "manual-list":
        return run_manual_list_cli(
            args.alias,
            Path(args.targets_file),
            account_store_factory=lambda: LocalAccountStore(resolve_standalone_data_root()),
        )
    if args.recruitment_command == "manual-handoff":
        return run_manual_handoff_cli(
            args.alias,
            Path(args.targets_file),
            args.select,
            args.confirm_target,
            Path(args.draft_file),
            args.approve_sha,
            None if args.context_file is None else Path(args.context_file),
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "manual-record":
        return run_manual_record_cli(
            args.alias,
            Path(args.targets_file),
            args.select,
            args.confirm_target,
            Path(args.draft_file),
            args.approve_sha,
            args.attempt_id,
            args.outcome,
            None if args.context_file is None else Path(args.context_file),
            data_root_factory=resolve_standalone_data_root,
        )
    if args.recruitment_command == "manual-status":
        return run_manual_status_cli(
            args.alias,
            Path(args.targets_file),
            args.select,
            args.confirm_target,
            data_root_factory=resolve_standalone_data_root,
        )
    return run_manual_review_cli(
        args.alias,
        Path(args.targets_file),
        args.select,
        Path(args.draft_file),
        None if args.context_file is None else Path(args.context_file),
        account_store_factory=lambda: LocalAccountStore(resolve_standalone_data_root()),
        confirm_target=args.confirm_target,
    )


def _handle_nurture_family_command(args: argparse.Namespace) -> int:
    if args.command == "nurture":
        return _handle_nurture_command(args)
    return _handle_browser_nurture_command(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    command_args = list(sys.argv[1:] if argv is None else argv)
    if command_args[:2] == ["recruitment", "browser-watch"]:
        parser.error = lambda message: _redacted_parser_error(parser, message)
    if command_args[:1] == ["browser-nurture"]:
        parser.error = lambda message: _redacted_parser_error(parser, message)
    args = parser.parse_args(command_args)
    try:
        if args.command == "workflow":
            plan = load_workflow(Path(args.file))
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            store.get(plan.account)
            output = asyncio.run(_run_workflow_command(plan, root, store))
            sys.stdout.write(output)
            return 0

        if args.command == "recurrence":
            root = resolve_standalone_data_root()
            accounts = LocalAccountStore(root)
            recurrences = LocalRecurrenceStore(root, accounts)
            if args.recurrence_command == "create":
                record = recurrences.create(Path(args.file), args.every_seconds)
                sys.stdout.write(
                    f"recurrence created id={record.id} account={record.account} "
                    f"every_seconds={record.interval_seconds} "
                    f"next_due_at={_format_recurrence_timestamp(record.next_due_at)}\n"
                )
                return 0
            if args.recurrence_command == "list":
                for record in recurrences.list():
                    sys.stdout.write(_format_recurrence_record(record))
                return 0
            if args.recurrence_command == "show":
                sys.stdout.write(_format_recurrence_record(recurrences.get(args.id)))
                return 0
            if args.recurrence_command == "disable":
                record = recurrences.disable(args.id)
                sys.stdout.write(f"recurrence disabled id={record.id}\n")
                return 0
            record = recurrences.get(args.id)
            if record.status != "ENABLED":
                raise StandaloneRecurrenceError("RECURRENCE_DISABLED")
            sys.stdout.write(f"recurrence starting id={record.id}\n")
            sys.stdout.flush()
            asyncio.run(_run_recurrence_command(args.id, root, accounts, recurrences))
            return 0

        if args.command == "post-carousel":
            manifest = load_carousel_manifest(Path(args.manifest))
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            sys.stdout.write(
                asyncio.run(_run_carousel_post_command(args.alias, manifest, root, store))
            )
            return 0

        if args.command == "moderate-reply":
            reply_id, action = validate_reply_moderation_inputs(args.reply_id, args.action)
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            store.get(args.alias)
            sys.stdout.write(
                asyncio.run(_run_moderate_reply_command(args.alias, reply_id, action, root, store))
            )
            return 0

        if args.command in {"nurture", "browser-nurture"}:
            return _handle_nurture_family_command(args)

        if args.command == "recruitment":
            return _handle_recruitment_command(args)

        root = resolve_standalone_data_root()
        store = LocalAccountStore(root)
        if args.command == "account":
            if args.account_command == "add":
                account = store.add(args.alias)
                sys.stdout.write(f"added {account.alias} {account.id}\n")
                return 0
            if args.account_command == "login":
                return _run_browser_command(args, root, store)
            if args.account_command == "credential":
                bind_env_credential(store, args.alias, args.variable_name)
                sys.stdout.write(f"credential configured {args.alias}\n")
                return 0
            for account in store.list():
                sys.stdout.write(f"{account.alias} {account.id}\n")
            return 0

        if args.command in {"profile", "thread", "feed", "browser"}:
            return _run_browser_command(args, root, store)

        if args.command == "operation":
            try:
                operation_id = UUID(args.operation_id)
            except ValueError:
                raise StandaloneMutationError("OPERATION_NOT_FOUND") from None
            operation = LocalOperationStore(root).get(operation_id)
            child_summary = (
                f" children={len(operation.child_container_ids)}"
                if operation.kind == "POST_CAROUSEL"
                else ""
            )
            moderation_summary = (
                f" action={operation.action}" if operation.kind == "MODERATE_REPLY" else ""
            )
            sys.stdout.write(
                f"operation {operation.id} kind={operation.kind} phase={operation.phase} "
                f"container={operation.container_id or '-'} "
                f"media={operation.media_id or '-'} "
                f"outcome={operation.outcome_code or '-'}"
                f"{child_summary}{moderation_summary}\n"
            )
            return 0

        if args.command == "post":
            sys.stdout.write(asyncio.run(_run_post_command(args, root, store)))
            return 0

        if args.command == "reply":
            sys.stdout.write(asyncio.run(_run_reply_command(args, root, store)))
            return 0

        if args.command == "post-image":
            sys.stdout.write(
                asyncio.run(_run_media_post_command(args, root, store, media_type="IMAGE"))
            )
            return 0

        if args.command == "post-video":
            sys.stdout.write(
                asyncio.run(_run_media_post_command(args, root, store, media_type="VIDEO"))
            )
            return 0

        sys.stdout.write(asyncio.run(_run_api_command(args, root, store)))
        return 0
    except ManualWorkflowError as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 130 if error.code.endswith("INTERRUPTED") else 1
    except (
        RecruitmentReplyAssistantError,
        ReplyProposalError,
        RecruitmentQualificationError,
        OpportunityQueueError,
        ManualOpportunityError,
        ManualOutcomeError,
        RecruitmentContentCliError,
        RecruitmentContentPlannerError,
        StandaloneAccountError,
        StandaloneApiError,
        ThreadsCredentialError,
        ThreadsAPIError,
        StandaloneMutationError,
    ) as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except NurtureRunnerError as error:
        suffix = f" run={error.run_id}" if error.run_id is not None else ""
        if error.operation_id is not None:
            suffix += f" operation={error.operation_id}"
        sys.stderr.write(f"ERROR {error.code}{suffix}\n")
        return 1
    except NurtureRunnerInterrupted as error:
        sys.stderr.write(f"ERROR {error.code} run={error.run_id}\n")
        return 130
    except StandaloneWorkflowError as error:
        suffix = f" step={error.step_index}" if error.step_index is not None else ""
        if error.operation_id is not None:
            suffix += f" operation={error.operation_id}"
        sys.stderr.write(f"ERROR {error.code}{suffix}\n")
        return 1
    except StandaloneRecurrenceError as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("ERROR INTERRUPTED\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
