"""Foreground coordinator for one explicit recruitment session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from threads_platform.api.contracts import (
    PublishingQuota,
    RemoteAuthenticatedProfile,
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
from threads_platform.standalone.api import StandaloneApiError
from threads_platform.standalone.app import build_standalone_app
from threads_platform.standalone.nurture import (
    AccountNurtureInboundConfig,
    NurturePresetError,
    NurturePresetV1,
    build_account_nurture_inbound_config,
    get_nurture_preset,
)
from threads_platform.standalone.nurture_content import (
    ContentCandidateV1,
    ContentCategory,
    NurtureContentError,
    load_content_candidate,
)
from threads_platform.standalone.nurture_draft import (
    NurtureDraft,
    NurtureDraftError,
    load_nurture_draft,
)
from threads_platform.standalone.nurture_runner import (
    NurturePreview,
    NurtureRunner,
    NurtureRunnerError,
    NurtureRunResult,
)
from threads_platform.standalone.nurture_store import NurtureRunV1, NurtureStore

_SAFE_ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


class RecruitmentSessionMode(StrEnum):
    VALIDATE = "validate"
    PREFLIGHT = "preflight"
    OBSERVE = "observe"
    REVIEW = "review"
    APPLY_REPLY = "apply_reply"
    APPLY_POST = "apply_post"


class RecruitmentSessionError(Exception):
    """A safe, bounded error code for foreground session failures."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_ERROR.fullmatch(code) is not None else "SESSION_FAILED"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"RecruitmentSessionError(code={self.code})"


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentContentPreview:
    """A bounded private preview of the supplied own-content candidate."""

    candidate_id: UUID
    category: ContentCategory
    source_kind: Literal["OPERATOR_SOURCE_ID"]
    source_id: str = field(repr=False)
    due: bool
    text: str = field(repr=False)
    action: Literal["POST_TEXT"] = "POST_TEXT"

    def __post_init__(self) -> None:
        if (
            type(self.candidate_id) is not UUID
            or type(self.category) is not str
            or type(self.source_kind) is not str
            or self.source_kind != "OPERATOR_SOURCE_ID"
            or type(self.source_id) is not str
            or not 1 <= len(self.source_id) <= 256
            or type(self.due) is not bool
            or type(self.text) is not str
            or len(self.text) > 500
            or type(self.action) is not str
            or self.action != "POST_TEXT"
        ):
            raise ValueError("invalid recruitment content preview")

    def __repr__(self) -> str:
        return (
            "RecruitmentContentPreview("
            f"candidate_id={self.candidate_id}, category={self.category}, "
            f"source_kind={self.source_kind}, source_id=<redacted>, due={self.due}, "
            "text=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentSessionResult:
    """Typed local result; private remote and proposed text stay out of repr/output."""

    mode: RecruitmentSessionMode
    status: str
    receipt: NurtureRunV1 | None = field(default=None, repr=False)
    inbound_status: str | None = None
    target_preview: NurturePreview | None = field(default=None, repr=False)
    content_preview: RecruitmentContentPreview | None = field(default=None, repr=False)
    credential_status: str | None = None
    publishing_quota_status: str | None = None
    reply_quota_status: str | None = None

    def __repr__(self) -> str:
        return (
            "RecruitmentSessionResult("
            f"mode={self.mode.value}, status={self.status}, "
            f"receipt={'present' if self.receipt is not None else 'absent'}, "
            f"target_preview={'present' if self.target_preview is not None else 'absent'}, "
            f"content_preview={'present' if self.content_preview is not None else 'absent'})"
        )


class RecruitmentSession:
    """Run validate, observe, review, and one explicitly approved effect in foreground."""

    def __init__(self, root: Path, accounts: LocalAccountStore | None = None) -> None:
        self._root = root
        self._accounts = accounts if accounts is not None else LocalAccountStore(root)

    def validate(
        self,
        account_alias: str,
        *,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        """Check local readiness without creating state, locks, clients, or receipts."""

        account, preset = self._validated_local_state(account_alias)
        _bound_inbound_config(account, preset, inbound_owner_username, inbound_thread_cap)
        return RecruitmentSessionResult(RecruitmentSessionMode.VALIDATE, "READY")

    async def preflight(self, account_alias: str) -> RecruitmentSessionResult:
        """Read token identity and both quota counters without writing local or remote state."""

        self._validated_local_state(account_alias)
        try:
            async with build_standalone_app(
                self._root,
                self._accounts,
                include_api=True,
                include_mutations=False,
                include_browser=False,
            ) as app:
                identity = await app.api.authenticated_profile(account_alias)
                if type(identity) is not RemoteAuthenticatedProfile:
                    raise RecruitmentSessionError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
                quota = await app.api.quota(account_alias)
        except RecruitmentSessionError:
            raise
        except (
            StandaloneApiError,
            StandaloneAccountError,
            ThreadsAPIError,
            ThreadsCredentialError,
        ) as error:
            raise RecruitmentSessionError(_error_code(error)) from None
        except Exception:  # noqa: BLE001 — never expose runtime or response details.
            raise RecruitmentSessionError("SESSION_FAILED") from None

        publishing_status = _quota_status(quota, reply=False)
        reply_status = _quota_status(quota, reply=True)
        status = (
            "OBSERVED"
            if publishing_status == "available" and reply_status == "available"
            else "STOP"
        )
        return RecruitmentSessionResult(
            RecruitmentSessionMode.PREFLIGHT,
            status,
            credential_status="verified",
            publishing_quota_status=publishing_status,
            reply_quota_status=reply_status,
        )

    def load_reply_draft(self, account_alias: str, path: Path) -> NurtureDraft:
        """Load one bounded local reply packet after validating its account context."""

        self._validated_local_state(account_alias)
        return load_nurture_draft(path)

    def load_content_candidate(self, account_alias: str, path: Path) -> ContentCandidateV1:
        """Load one bounded account and preset-bound own-content packet."""

        account, preset = self._validated_local_state(account_alias)
        return load_content_candidate(
            path,
            account_alias=account.alias,
            preset_id=preset.id,
        )

    async def observe(
        self,
        account_alias: str,
        *,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        """Discover bounded remote candidates and persist only local observation state."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.OBSERVE,
            inbound_owner_username=inbound_owner_username,
            inbound_thread_cap=inbound_thread_cap,
        )

    async def review(
        self,
        account_alias: str,
        *,
        draft: NurtureDraft | None = None,
        content_candidate: ContentCandidateV1 | None = None,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        """Return a bounded preview without constructing mutation runtime."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.REVIEW,
            draft=draft,
            content_candidate=content_candidate,
            inbound_owner_username=inbound_owner_username,
            inbound_thread_cap=inbound_thread_cap,
        )

    async def apply_reply(
        self,
        account_alias: str,
        draft: NurtureDraft,
        *,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        """Apply only the exact approved reply draft through the existing journal."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.APPLY_REPLY,
            draft=draft,
            inbound_owner_username=inbound_owner_username,
            inbound_thread_cap=inbound_thread_cap,
        )

    async def apply_post(
        self,
        account_alias: str,
        content_candidate: ContentCandidateV1,
        *,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        """Apply only the exact approved own-content candidate through the journal."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.APPLY_POST,
            content_candidate=content_candidate,
            inbound_owner_username=inbound_owner_username,
            inbound_thread_cap=inbound_thread_cap,
        )

    async def _run(
        self,
        account_alias: str,
        mode: RecruitmentSessionMode,
        *,
        draft: NurtureDraft | None = None,
        content_candidate: ContentCandidateV1 | None = None,
        inbound_owner_username: str | None = None,
        inbound_thread_cap: int | None = None,
    ) -> RecruitmentSessionResult:
        account, preset = self._validated_local_state(account_alias)
        apply_requested = mode in {
            RecruitmentSessionMode.APPLY_REPLY,
            RecruitmentSessionMode.APPLY_POST,
        }
        if (mode is RecruitmentSessionMode.APPLY_REPLY and draft is None) or (
            mode not in {RecruitmentSessionMode.APPLY_REPLY, RecruitmentSessionMode.REVIEW}
            and draft is not None
        ):
            raise RecruitmentSessionError("INVALID_NURTURE_DRAFT")
        if (mode is RecruitmentSessionMode.APPLY_POST and content_candidate is None) or (
            mode not in {RecruitmentSessionMode.APPLY_POST, RecruitmentSessionMode.REVIEW}
            and content_candidate is not None
        ):
            raise RecruitmentSessionError("INVALID_NURTURE_CONTENT")
        if draft is not None and content_candidate is not None:
            raise RecruitmentSessionError("NURTURE_INPUT_CONFLICT")

        inbound_config = _bound_inbound_config(
            account,
            preset,
            inbound_owner_username,
            inbound_thread_cap,
        )
        preview_only = mode is RecruitmentSessionMode.REVIEW and draft is not None
        try:
            async with build_standalone_app(
                self._root,
                self._accounts,
                include_api=True,
                include_mutations=apply_requested,
                include_browser=False,
            ) as app:
                if apply_requested and app.mutations is None:
                    raise RecruitmentSessionError("SESSION_MUTATION_RUNTIME_UNAVAILABLE")
                result = await NurtureRunner(app.api, NurtureStore(self._root)).run_with_selection(
                    account,
                    preset,
                    apply_requested=apply_requested,
                    draft=draft,
                    content_candidate=content_candidate,
                    mutations=app.mutations,
                    inbound_config=inbound_config,
                    preview_only=preview_only,
                )
        except RecruitmentSessionError:
            raise
        except (NurtureRunnerError, StandaloneAccountError, NurturePresetError) as error:
            raise RecruitmentSessionError(_error_code(error)) from None
        except Exception:  # noqa: BLE001 — keep unexpected runtime details out of the session API.
            raise RecruitmentSessionError("SESSION_FAILED") from None
        return _session_result(mode, result, content_candidate)

    def _validated_local_state(self, account_alias: str) -> tuple[LocalAccount, NurturePresetV1]:
        try:
            root_info = self._root.lstat()
        except FileNotFoundError:
            raise RecruitmentSessionError("LOCAL_DATA_ROOT_NOT_FOUND") from None
        except OSError:
            raise RecruitmentSessionError("LOCAL_DATA_ROOT_UNAVAILABLE") from None
        if not stat.S_ISDIR(root_info.st_mode) or not os.access(self._root, os.R_OK | os.W_OK):
            raise RecruitmentSessionError("LOCAL_DATA_ROOT_UNAVAILABLE")

        try:
            preset = get_nurture_preset("recruitment")
            account = self._accounts.get(account_alias)
        except (StandaloneAccountError, NurturePresetError) as error:
            raise RecruitmentSessionError(_error_code(error)) from None
        return account, preset


def _bound_inbound_config(
    account: LocalAccount,
    preset: NurturePresetV1,
    owner_public_username: str | None,
    max_owned_threads: int | None,
) -> AccountNurtureInboundConfig | None:
    if owner_public_username is None and max_owned_threads is None:
        return None
    if (
        owner_public_username is None
        or type(max_owned_threads) is not int
        or not 1 <= max_owned_threads <= 10
    ):
        raise RecruitmentSessionError("INBOUND_CONFIGURATION_INVALID")
    try:
        return build_account_nurture_inbound_config(
            preset,
            account.alias,
            owner_public_username,
            max_owned_threads,
        )
    except TypeError, ValueError:
        raise RecruitmentSessionError("INBOUND_CONFIGURATION_INVALID") from None


def _session_result(
    mode: RecruitmentSessionMode,
    result: NurtureRunResult,
    content_candidate: ContentCandidateV1 | None,
) -> RecruitmentSessionResult:
    content_preview: RecruitmentContentPreview | None = None
    if mode is RecruitmentSessionMode.REVIEW and content_candidate is not None:
        decisions = result.receipt.decision_codes
        if "CONTENT_RECOMMENDED_NO_APPLY" in decisions:
            content_preview = RecruitmentContentPreview(
                content_candidate.candidate_id,
                content_candidate.category,
                content_candidate.source_kind,
                content_candidate.source_id,
                True,
                content_candidate.text,
            )
        elif "CONTENT_NOT_DUE" in decisions:
            content_preview = RecruitmentContentPreview(
                content_candidate.candidate_id,
                content_candidate.category,
                content_candidate.source_kind,
                content_candidate.source_id,
                False,
                content_candidate.text,
            )
    return RecruitmentSessionResult(
        mode=mode,
        status=result.receipt.outcome,
        receipt=result.receipt,
        inbound_status=result.inbound_status.value,
        target_preview=result.preview,
        content_preview=content_preview,
    )


def _error_code(error: object) -> str:
    code = getattr(error, "code", None)
    return (
        code if type(code) is str and _SAFE_ERROR.fullmatch(code) is not None else "SESSION_FAILED"
    )


def _quota_status(quota: object, *, reply: bool) -> str:
    if type(quota) is not PublishingQuota:
        raise RecruitmentSessionError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    usage, total = (quota.reply_usage, quota.reply_total) if reply else (quota.usage, quota.total)
    if (
        usage is None
        or total is None
        or type(usage) is not int
        or type(total) is not int
        or usage < 0
        or total < 0
    ):
        return "unverified"
    return "exhausted" if usage >= total else "available"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_recruitment")
    parser.add_argument("--account", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--review", action="store_true")
    mode.add_argument("--apply-reply-file")
    mode.add_argument("--apply-post-file")
    review_input = parser.add_mutually_exclusive_group()
    review_input.add_argument("--review-reply-file")
    review_input.add_argument("--review-post-file")
    parser.add_argument(
        "--inbound-owner-username",
        help="Ephemeral claimed public username for this account's inbound read.",
    )
    parser.add_argument(
        "--inbound-thread-cap",
        help="Bound the inbound profile read to 1–10 owned threads for this invocation.",
    )
    return parser


def _format_result(result: RecruitmentSessionResult) -> str:
    if result.mode is RecruitmentSessionMode.VALIDATE:
        return "recruitment-session mode=validate status=READY\n"
    if result.mode is RecruitmentSessionMode.PREFLIGHT:
        return (
            "recruitment-session mode=preflight "
            f"status={result.status} credential={result.credential_status} "
            f"post_quota={result.publishing_quota_status} "
            f"reply_quota={result.reply_quota_status}\n"
        )
    receipt = result.receipt
    decisions = (
        "NO_ACTION"
        if receipt is None or not receipt.decision_codes
        else ",".join(receipt.decision_codes)
    )
    target = "available" if result.target_preview is not None else "none"
    content = (
        "unrequested"
        if result.content_preview is None
        else "due"
        if result.content_preview.due
        else "not_due"
    )
    inbound = result.inbound_status or "UNKNOWN"
    return (
        f"recruitment-session mode={result.mode.value} status={result.status} "
        f"decisions={decisions} target_preview={target} "
        f"own_content={content} inbound={inbound}\n"
    )


def _private_terminal_available(*, require_input: bool) -> bool:
    ci_enabled = any(
        os.environ.get(name, "").strip().casefold() in {"1", "true", "yes"}
        for name in ("CI", "GITHUB_ACTIONS")
    )
    if ci_enabled or not sys.stderr.isatty():
        return False
    return not require_input or sys.stdin.isatty()


def _private_review_text(
    account_alias: str,
    result: RecruitmentSessionResult,
) -> str:
    lines = ["PRIVATE LOCAL REVIEW — do not copy into public logs"]
    lines.append(f"account alias: {account_alias}")
    if result.target_preview is not None:
        target = result.target_preview
        lines.extend(
            (
                "target effect:",
                f"  fingerprint: {target.target_fingerprint}",
                f"  source: {target.source}",
                f"  reason codes: {', '.join(target.reason_codes) or 'none'}",
                f"  action: {target.action}",
                "  target context: "
                + (
                    "<unavailable>"
                    if target.target_text is None
                    else json.dumps(target.target_text, ensure_ascii=False)
                ),
                "  proposed reply: "
                + (
                    "<none>"
                    if target.proposed_text is None
                    else json.dumps(target.proposed_text, ensure_ascii=False)
                ),
            )
        )
    else:
        lines.append("target effect: <none selected>")

    if result.content_preview is not None:
        content = result.content_preview
        lines.extend(
            (
                "own-content effect:",
                f"  action: {content.action}",
                f"  candidate: {content.candidate_id}",
                f"  source kind: {content.source_kind}",
                f"  source id: {json.dumps(content.source_id, ensure_ascii=False)}",
                f"  category: {content.category}",
                f"  due: {str(content.due).lower()}",
                f"  proposed post: {json.dumps(content.text, ensure_ascii=False)}",
            )
        )
    return "\n".join(lines) + "\n"


def _write_private_review(account_alias: str, result: RecruitmentSessionResult) -> None:
    if not _private_terminal_available(require_input=False):
        raise RecruitmentSessionError("PRIVATE_REVIEW_TERMINAL_REQUIRED")
    sys.stderr.write(_private_review_text(account_alias, result))
    sys.stderr.flush()


def _confirm_private_apply() -> bool:
    if not _private_terminal_available(require_input=True):
        raise RecruitmentSessionError("PRIVATE_REVIEW_TERMINAL_REQUIRED")
    sys.stderr.write("\nType APPLY to approve this exact effect (anything else cancels): ")
    sys.stderr.flush()
    return sys.stdin.readline(16).strip() == "APPLY"


def _not_applied_result(
    reviewed: RecruitmentSessionResult,
    status: str,
) -> RecruitmentSessionResult:
    return RecruitmentSessionResult(
        mode=RecruitmentSessionMode.REVIEW,
        status=status,
        receipt=reviewed.receipt,
        inbound_status=reviewed.inbound_status,
        target_preview=reviewed.target_preview,
        content_preview=reviewed.content_preview,
    )


async def _review_and_apply(
    session: RecruitmentSession,
    account_alias: str,
    *,
    draft: NurtureDraft | None = None,
    content_candidate: ContentCandidateV1 | None = None,
    inbound_owner_username: str | None = None,
    inbound_thread_cap: int | None = None,
) -> RecruitmentSessionResult:
    reviewed = await session.review(
        account_alias,
        draft=draft,
        content_candidate=content_candidate,
        inbound_owner_username=inbound_owner_username,
        inbound_thread_cap=inbound_thread_cap,
    )
    _write_private_review(account_alias, reviewed)

    if draft is not None:
        preview = reviewed.target_preview
        if (
            preview is None
            or preview.target_fingerprint != draft.target_fingerprint
            or preview.action != "REPLY"
            or preview.proposed_text != draft.text
        ):
            raise RecruitmentSessionError("NURTURE_DRAFT_TARGET_MISMATCH")
    elif content_candidate is not None:
        preview = reviewed.content_preview
        if (
            preview is None
            or preview.candidate_id != content_candidate.candidate_id
            or preview.source_kind != content_candidate.source_kind
            or preview.source_id != content_candidate.source_id
            or preview.category != content_candidate.category
            or preview.text != content_candidate.text
            or preview.action != "POST_TEXT"
        ):
            raise RecruitmentSessionError("INVALID_NURTURE_CONTENT")
        if not preview.due:
            return _not_applied_result(reviewed, "NOT_DUE")
    else:
        raise RecruitmentSessionError("SESSION_MODE_REQUIRED")

    if not _confirm_private_apply():
        return _not_applied_result(reviewed, "DECLINED")
    if draft is not None:
        return await session.apply_reply(
            account_alias,
            draft,
            inbound_owner_username=inbound_owner_username,
            inbound_thread_cap=inbound_thread_cap,
        )
    assert content_candidate is not None
    return await session.apply_post(
        account_alias,
        content_candidate,
        inbound_owner_username=inbound_owner_username,
        inbound_thread_cap=inbound_thread_cap,
    )


def _inbound_thread_cap(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        cap = int(raw)
    except TypeError, ValueError:
        raise RecruitmentSessionError("INBOUND_CONFIGURATION_INVALID") from None
    if not 1 <= cap <= 10:
        raise RecruitmentSessionError("INBOUND_CONFIGURATION_INVALID")
    return cap


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode_count = sum((args.validate, args.preflight, args.observe, args.review))
    if (
        args.review_reply_file is not None or args.review_post_file is not None
    ) and not args.review:
        sys.stderr.write("ERROR REVIEW_INPUT_REQUIRES_REVIEW_MODE\n")
        return 1
    if mode_count == 0 and args.apply_reply_file is None and args.apply_post_file is None:
        sys.stderr.write("ERROR SESSION_MODE_REQUIRED\n")
        return 1
    applying = args.apply_reply_file is not None or args.apply_post_file is not None
    if (args.review or applying) and not _private_terminal_available(require_input=applying):
        sys.stderr.write("ERROR PRIVATE_REVIEW_TERMINAL_REQUIRED\n")
        return 1

    try:
        root = resolve_standalone_data_root()
        session = RecruitmentSession(root)
        inbound_owner = args.inbound_owner_username
        inbound_cap = _inbound_thread_cap(args.inbound_thread_cap)
        if args.validate:
            result = session.validate(
                args.account,
                inbound_owner_username=inbound_owner,
                inbound_thread_cap=inbound_cap,
            )
        elif args.preflight:
            if inbound_owner is not None or inbound_cap is not None:
                raise RecruitmentSessionError("INBOUND_CONFIGURATION_NOT_APPLICABLE")
            result = asyncio.run(session.preflight(args.account))
        elif args.observe:
            result = asyncio.run(
                session.observe(
                    args.account,
                    inbound_owner_username=inbound_owner,
                    inbound_thread_cap=inbound_cap,
                )
            )
        elif args.review:
            draft = None
            candidate = None
            if args.review_reply_file is not None:
                draft = session.load_reply_draft(args.account, Path(args.review_reply_file))
            if args.review_post_file is not None:
                candidate = session.load_content_candidate(
                    args.account, Path(args.review_post_file)
                )
            result = asyncio.run(
                session.review(
                    args.account,
                    draft=draft,
                    content_candidate=candidate,
                    inbound_owner_username=inbound_owner,
                    inbound_thread_cap=inbound_cap,
                )
            )
            _write_private_review(args.account, result)
        elif args.apply_reply_file is not None:
            draft = session.load_reply_draft(args.account, Path(args.apply_reply_file))
            result = asyncio.run(
                _review_and_apply(
                    session,
                    args.account,
                    draft=draft,
                    inbound_owner_username=inbound_owner,
                    inbound_thread_cap=inbound_cap,
                )
            )
        else:
            assert args.apply_post_file is not None
            candidate = session.load_content_candidate(args.account, Path(args.apply_post_file))
            result = asyncio.run(
                _review_and_apply(
                    session,
                    args.account,
                    content_candidate=candidate,
                    inbound_owner_username=inbound_owner,
                    inbound_thread_cap=inbound_cap,
                )
            )
    except RecruitmentSessionError as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except (
        StandaloneAccountError,
        NurturePresetError,
        NurtureDraftError,
        NurtureContentError,
    ) as error:
        sys.stderr.write(f"ERROR {_error_code(error)}\n")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 — never echo paths, remote data, or secret details.
        sys.stderr.write("ERROR SESSION_FAILED\n")
        return 1
    sys.stdout.write(_format_result(result))
    return 1 if result.mode is RecruitmentSessionMode.PREFLIGHT and result.status == "STOP" else 0


__all__ = [
    "RecruitmentContentPreview",
    "RecruitmentSession",
    "RecruitmentSessionError",
    "RecruitmentSessionMode",
    "RecruitmentSessionResult",
    "main",
]
