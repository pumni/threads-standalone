"""CLI for local account metadata and read-only Threads API commands."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
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
from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
from threads_platform.standalone.api import StandaloneApiError, bind_env_credential
from threads_platform.standalone.app import build_standalone_app
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


def _format_discovery_page(kind: str, page: DiscoveryPage, limit: int) -> str:
    threads = page.threads[: min(max(limit, 0), 50)]
    lines = [
        (
            f"{kind} count={len(threads)} "
            f"has_more={str(page.has_more).lower()} "
            f"next_cursor={_format_discovery_cursor(page.next_cursor)}\n"
        )
    ]
    for thread in threads:
        timestamp = thread.timestamp.isoformat() if thread.timestamp is not None else None
        quote = "-" if thread.is_quote_post is None else str(thread.is_quote_post).lower()
        has_replies = "-" if thread.has_replies is None else str(thread.has_replies).lower()
        lines.append(
            f"thread {_format_bounded_text(thread.remote_thread_id, 255)} "
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
        f"selected={run.selected_count} decision={decision}{target}{candidate}\n"
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
    return parser


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
        if args.command == "account":
            asyncio.run(runtime.login(args.alias))
            sys.stdout.write(f"login browser closed {args.alias}\n")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
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

        if args.command == "nurture":
            return _handle_nurture_command(args)

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

        if args.command in {"profile", "thread", "feed"}:
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
    except (
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
    except KeyboardInterrupt:
        sys.stderr.write("ERROR INTERRUPTED\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
