"""Foreground coordinator for one explicit recruitment session."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
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
    due: bool
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.candidate_id) is not UUID
            or type(self.category) is not str
            or type(self.due) is not bool
            or type(self.text) is not str
            or len(self.text) > 500
        ):
            raise ValueError("invalid recruitment content preview")

    def __repr__(self) -> str:
        return (
            "RecruitmentContentPreview("
            f"candidate_id={self.candidate_id}, category={self.category}, due={self.due}, "
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

    def validate(self, account_alias: str) -> RecruitmentSessionResult:
        """Check local readiness without creating state, locks, clients, or receipts."""

        _account, _preset = self._validated_local_state(account_alias)
        return RecruitmentSessionResult(RecruitmentSessionMode.VALIDATE, "READY")

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

    async def observe(self, account_alias: str) -> RecruitmentSessionResult:
        """Discover bounded remote candidates and persist only local observation state."""

        return await self._run(account_alias, RecruitmentSessionMode.OBSERVE)

    async def review(
        self,
        account_alias: str,
        *,
        content_candidate: ContentCandidateV1 | None = None,
    ) -> RecruitmentSessionResult:
        """Return a bounded private preview without constructing mutation runtime."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.REVIEW,
            content_candidate=content_candidate,
        )

    async def apply_reply(
        self,
        account_alias: str,
        draft: NurtureDraft,
    ) -> RecruitmentSessionResult:
        """Apply only the exact approved reply draft through the existing journal."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.APPLY_REPLY,
            draft=draft,
        )

    async def apply_post(
        self,
        account_alias: str,
        content_candidate: ContentCandidateV1,
    ) -> RecruitmentSessionResult:
        """Apply only the exact approved own-content candidate through the journal."""

        return await self._run(
            account_alias,
            RecruitmentSessionMode.APPLY_POST,
            content_candidate=content_candidate,
        )

    async def _run(
        self,
        account_alias: str,
        mode: RecruitmentSessionMode,
        *,
        draft: NurtureDraft | None = None,
        content_candidate: ContentCandidateV1 | None = None,
    ) -> RecruitmentSessionResult:
        account, preset = self._validated_local_state(account_alias)
        apply_requested = mode in {
            RecruitmentSessionMode.APPLY_REPLY,
            RecruitmentSessionMode.APPLY_POST,
        }
        if (mode is RecruitmentSessionMode.APPLY_REPLY) != (draft is not None):
            raise RecruitmentSessionError("INVALID_NURTURE_DRAFT")
        if (mode is RecruitmentSessionMode.APPLY_POST and content_candidate is None) or (
            mode not in {RecruitmentSessionMode.APPLY_POST, RecruitmentSessionMode.REVIEW}
            and content_candidate is not None
        ):
            raise RecruitmentSessionError("INVALID_NURTURE_CONTENT")

        inbound_config = _bound_inbound_config(account, preset)
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
) -> AccountNurtureInboundConfig | None:
    if preset.owner_public_username is None:
        return None
    try:
        return build_account_nurture_inbound_config(
            preset,
            account.alias,
            preset.owner_public_username,
            preset.max_owned_threads_inspected_per_explicit_run,
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
                True,
                content_candidate.text,
            )
        elif "CONTENT_NOT_DUE" in decisions:
            content_preview = RecruitmentContentPreview(
                content_candidate.candidate_id,
                content_candidate.category,
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_recruitment")
    parser.add_argument("--account", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--review", action="store_true")
    mode.add_argument("--apply-reply-file")
    mode.add_argument("--apply-post-file")
    parser.add_argument("--review-post-file")
    return parser


def _format_result(result: RecruitmentSessionResult) -> str:
    if result.mode is RecruitmentSessionMode.VALIDATE:
        return "recruitment-session mode=validate status=READY\n"
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode_count = sum((args.validate, args.observe, args.review))
    if args.review_post_file is not None and not args.review:
        sys.stderr.write("ERROR REVIEW_INPUT_REQUIRES_REVIEW_MODE\n")
        return 1
    if mode_count == 0 and args.apply_reply_file is None and args.apply_post_file is None:
        sys.stderr.write("ERROR SESSION_MODE_REQUIRED\n")
        return 1

    try:
        root = resolve_standalone_data_root()
        session = RecruitmentSession(root)
        if args.validate:
            result = session.validate(args.account)
        elif args.observe:
            result = asyncio.run(session.observe(args.account))
        elif args.review:
            candidate = None
            if args.review_post_file is not None:
                candidate = session.load_content_candidate(
                    args.account, Path(args.review_post_file)
                )
            result = asyncio.run(session.review(args.account, content_candidate=candidate))
        elif args.apply_reply_file is not None:
            draft = session.load_reply_draft(args.account, Path(args.apply_reply_file))
            result = asyncio.run(session.apply_reply(args.account, draft))
        else:
            candidate = session.load_content_candidate(args.account, Path(args.apply_post_file))
            result = asyncio.run(session.apply_post(args.account, candidate))
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
    return 0


__all__ = [
    "RecruitmentContentPreview",
    "RecruitmentSession",
    "RecruitmentSessionError",
    "RecruitmentSessionMode",
    "RecruitmentSessionResult",
    "main",
]
