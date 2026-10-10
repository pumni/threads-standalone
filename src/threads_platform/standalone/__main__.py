"""CLI for local account metadata and read-only Threads API commands."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

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
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
from threads_platform.standalone.api import StandaloneApiError, bind_env_credential
from threads_platform.standalone.app import build_standalone_app


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-local")
    commands = parser.add_subparsers(dest="command", required=True)

    account_parser = commands.add_parser("account")
    account_commands = account_parser.add_subparsers(dest="account_command", required=True)
    add_parser = account_commands.add_parser("add")
    add_parser.add_argument("alias")
    account_commands.add_parser("list")
    credential_parser = account_commands.add_parser("credential")
    credential_commands = credential_parser.add_subparsers(dest="credential_command", required=True)
    set_env_parser = credential_commands.add_parser("set-env")
    set_env_parser.add_argument("alias")
    set_env_parser.add_argument("variable_name")

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        root = resolve_standalone_data_root()
        store = LocalAccountStore(root)
        if args.command == "account":
            if args.account_command == "add":
                account = store.add(args.alias)
                sys.stdout.write(f"added {account.alias} {account.id}\n")
                return 0
            if args.account_command == "credential":
                bind_env_credential(store, args.alias, args.variable_name)
                sys.stdout.write(f"credential configured {args.alias}\n")
                return 0
            for account in store.list():
                sys.stdout.write(f"{account.alias} {account.id}\n")
            return 0

        sys.stdout.write(asyncio.run(_run_api_command(args, root, store)))
        return 0
    except (
        StandaloneAccountError,
        StandaloneApiError,
        ThreadsCredentialError,
        ThreadsAPIError,
    ) as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("ERROR INTERRUPTED\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
