"""Run an end-to-end synthetic recruitment review without live services."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import tempfile
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO, cast

from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    PublishingQuota,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    ReplyPage,
    ThreadsTransportError,
)
from threads_platform.standalone import recruitment_session as recruitment_module
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture_runner import (
    NurtureShortlistCandidate,
    NurtureShortlistResult,
)
from threads_platform.standalone.recruitment_reply_proposal import (
    HumanReplyEditorialSession,
    ReplyProposalError,
    ReplyProposalInput,
    create_human_reply_preview,
    evaluate_synthetic_reply_quality,
)
from threads_platform.standalone.recruitment_session import (
    RecruitmentSession,
    RecruitmentSessionError,
)
from threads_platform.standalone.recruitment_watch import (
    DEFAULT_WATCH_INTERVAL_MINUTES,
    WatchRunResult,
    WatchTiming,
)

_MAX_OUTPUT_CHARS = 8_192
_MAX_ACTION_CHARS = 16
_MAX_EDIT_CHARS = 500
_MAX_EDITOR_EDITS = 5
_WATCHED_TEXT = "SYNTHETIC TARGET W: A recruiter asks how a new graduate can prepare a career CV?"
_FRESH_TEXT = (
    "SYNTHETIC TARGET F: Career question? Which interview project should an intern highlight?"
)
_FAKE_REPLIES = {
    _FRESH_TEXT: "Which project best shows the skills you used and what you learned?",
}
_EDITED_REPLIES = {
    _FRESH_TEXT: "Which project best shows the skills you used, the choices you made, and what you learned?",
}
_LOW_INFORMATION_REPLY = "i dont know"
_EMPTY_PAGE = DiscoveryPage((), None, False)


class _InputLineTooLong(Exception):
    """A bounded input line was rejected without retaining its contents."""


@dataclass(frozen=True, slots=True)
class JourneyResult:
    status: Literal["COMPLETED", "STOP"]
    reason: str | None
    watch: WatchRunResult
    shortlist_count: int
    selected_label: str | None
    editorial_disposition: str
    proposal_created: bool
    receipt_count: int
    receipt_bytes: int
    target_state_bytes: int
    remote_mutations: int


class _SyntheticTiming(WatchTiming):
    def __init__(self, now: datetime) -> None:
        self._now = now
        self._monotonic = 0.0
        self.fake_wait_seconds = 0

    def monotonic(self) -> float:
        return self._monotonic

    def utc_now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.fake_wait_seconds += int(seconds)
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class _SyntheticReadOnlyApi:
    """Phase-aware fake API with no credential, network, or mutation methods."""

    def __init__(
        self,
        now: datetime,
        *,
        include_fresh_candidate: bool = True,
        fail_preflight: bool = False,
        fail_watch_read: bool = False,
        fail_shortlist_read: bool = False,
    ) -> None:
        self.watch_thread = _thread("ao05a_synthetic_watch_w", _WATCHED_TEXT, now)
        self.fresh_thread = _thread("ao05a_synthetic_fresh_f", _FRESH_TEXT, now)
        self.phase: Literal["watch", "shortlist"] = "watch"
        self.include_fresh_candidate = include_fresh_candidate
        self.fail_preflight = fail_preflight
        self.fail_watch_read = fail_watch_read
        self.fail_shortlist_read = fail_shortlist_read
        self.read_calls = 0
        self.remote_mutations = 0
        self._keyword_result_returned = False

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        self.read_calls += 1
        if self.fail_preflight:
            raise ThreadsTransportError()
        return RemoteAuthenticatedProfile("ao05a_synthetic_owner", "synthetic_owner")

    async def quota(self, _alias: str) -> PublishingQuota:
        self.read_calls += 1
        return PublishingQuota(usage=0, total=100, reply_usage=0, reply_total=50)

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        if after is not None or limit <= 0:
            raise ValueError("invalid synthetic read request")
        self.read_calls += 1
        return _EMPTY_PAGE

    async def search(
        self,
        _alias: str,
        _query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        if (
            search_mode not in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
            or search_type is not DiscoverySearchType.RECENT
            or after is not None
            or limit <= 0
        ):
            raise ValueError("invalid synthetic read request")
        self.read_calls += 1
        if search_mode is not DiscoverySearchMode.KEYWORD or self._keyword_result_returned:
            return _EMPTY_PAGE
        self._keyword_result_returned = True
        if self.phase == "watch" and self.fail_watch_read:
            raise ThreadsTransportError()
        if self.phase == "shortlist" and self.fail_shortlist_read:
            raise ThreadsTransportError()
        if self.phase == "watch":
            return DiscoveryPage((self.watch_thread,), None, False)
        threads = (self.watch_thread, self.fresh_thread)
        if not self.include_fresh_candidate:
            threads = (self.watch_thread,)
        return DiscoveryPage(threads, None, False)

    async def profile_posts(
        self,
        _alias: str,
        _username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        if after is not None or limit <= 0:
            raise ValueError("invalid synthetic read request")
        self.read_calls += 1
        return _EMPTY_PAGE

    async def conversation(self, _alias: str, _thread_id: str, after: str | None) -> ReplyPage:
        if after is not None:
            raise ValueError("invalid synthetic read request")
        self.read_calls += 1
        return ReplyPage((), None, False)

    def start_fresh_shortlist(self) -> None:
        self.phase = "shortlist"
        self._keyword_result_returned = False


class _FakeReplyGenerator:
    """Return one fixed reply for the new synthetic candidate only."""

    def __init__(self) -> None:
        self.requests: list[ReplyProposalInput] = []

    async def generate_reply(self, request: ReplyProposalInput) -> str:
        self.requests.append(request)
        try:
            return _FAKE_REPLIES[request.candidate_text]
        except KeyError:
            raise RuntimeError("unknown synthetic candidate") from None


def _thread(remote_id: str, text: str, now: datetime) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_thread_id=remote_id,
        author_remote_id=f"{remote_id}_author",
        username="synthetic_author",
        text=text,
        permalink=None,
        media_type=None,
        timestamp=now - timedelta(minutes=5),
        is_quote_post=False,
        has_replies=True,
    )


def _fake_builder(
    expected_root: Path,
    expected_accounts: LocalAccountStore,
    api: _SyntheticReadOnlyApi,
):
    @asynccontextmanager
    async def build(
        app_root: Path,
        accounts: LocalAccountStore | None = None,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = False,
    ) -> AsyncGenerator[StandaloneAppContext]:
        if (
            accounts is not expected_accounts
            or app_root.resolve() != expected_root.resolve()
            or not include_api
            or include_mutations
            or include_browser
        ):
            raise RuntimeError("synthetic journey permits fake API reads only")
        if accounts.get("demo").credential_ref is not None:
            raise RuntimeError("synthetic account must not contain credentials")
        yield StandaloneAppContext(
            accounts,
            cast(LocalThreadsApiRuntime, api),
            None,
        )

    return build


def run_journey(
    root: Path,
    accounts: LocalAccountStore,
    api: _SyntheticReadOnlyApi,
    timing: _SyntheticTiming,
    *,
    editor_mode: Literal["scripted", "interactive"],
    input_stream: TextIO,
    output_stream: TextIO,
) -> JourneyResult:
    """Use existing watch, Nurture shortlist, proposal, and editor contracts."""

    session = RecruitmentSession(root, accounts)
    writes: list[int] = [0]

    def emit(value: str) -> None:
        _write_bounded(output_stream, value + "\n", writes)

    original_builder = recruitment_module.build_standalone_app
    recruitment_module.build_standalone_app = _fake_builder(root, accounts, api)  # type: ignore[assignment]
    try:
        emit("AO-05A SYNTHETIC FOREGROUND RECRUITMENT JOURNEY")
        emit("backend=FAKE_API identity=FAKE credentials=NONE root=TEMPORARY")
        watch = asyncio.run(
            session.watch_observe(
                "demo",
                interval_minutes=DEFAULT_WATCH_INTERVAL_MINUTES,
                max_cycles=1,
                timing=timing,
                reporter=lambda value: emit(f"watch {value}"),
            )
        )
        if watch.status != "COMPLETED" or watch.cycles != 1:
            emit(f"JOURNEY_STATUS=STOP reason={watch.reason_code or 'WATCH_STOP'}")
            return _result(root, accounts, watch, api, None, "DISCARDED", False, 0, "WATCH_STOP")

        receipt_count, receipt_bytes, state_bytes = _nurture_storage_summary(root, accounts)
        if receipt_count != 1 or state_bytes <= 0:
            emit("JOURNEY_STATUS=STOP reason=WATCH_STATE_UNAVAILABLE")
            return _result(
                root,
                accounts,
                watch,
                api,
                None,
                "DISCARDED",
                False,
                0,
                "WATCH_STATE_UNAVAILABLE",
            )
        emit(
            "WATCH_COMPLETED=YES receipt_count=1 cooldown_state=WRITTEN "
            f"receipt_bytes={receipt_bytes} state_bytes={state_bytes} fake_wait_seconds=0"
        )

        api.start_fresh_shortlist()
        try:
            shortlist = asyncio.run(session.shortlist("demo", now=datetime.now(UTC), limit=5))
        except RecruitmentSessionError as error:
            emit(f"FRESH_SHORTLIST=STOP reason={error.code}")
            return _result(
                root,
                accounts,
                watch,
                api,
                None,
                "DISCARDED",
                False,
                0,
                "SHORTLIST_STOP",
            )
        if shortlist.inbound_status.value not in {"INBOUND_NOT_CONFIGURED", "INBOUND_READY"}:
            emit("FRESH_SHORTLIST=STOP reason=INBOUND_UNAVAILABLE")
            return _result(
                root,
                accounts,
                watch,
                api,
                shortlist,
                "DISCARDED",
                False,
                0,
                "SHORTLIST_STOP",
            )

        emit(f"FRESH_SHORTLIST=READY candidate_count={len(shortlist.candidates)}")
        displayed_candidates = tuple(
            (candidate, candidate.fingerprint) for candidate in shortlist.candidates
        )
        fresh_selection: int | None = None
        for index, candidate in enumerate(shortlist.candidates, start=1):
            label = _candidate_label(candidate)
            emit(
                f"{index}. candidate={label} score={candidate.score if candidate.score is not None else '—'} "
                f"reasons={','.join(candidate.reason_codes) or 'NONE'} "
                f"status={candidate.status} action={candidate.action_hint}"
            )
            if label == "W" and candidate.status == "COOLDOWN":
                emit("CANDIDATE_W=COOLDOWN — NO PROPOSAL")
            if label == "F" and candidate.status == "ELIGIBLE":
                fresh_selection = index

        if fresh_selection is None:
            emit("NO_ELIGIBLE_CANDIDATE=YES")
            emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
            emit("DRAFT_PERSISTED=NO remote_mutations=0")
            _assert_no_private_persistence(root, api, ())
            return _result(
                root,
                accounts,
                watch,
                api,
                shortlist,
                "DISCARDED",
                False,
                len(shortlist.candidates),
                None,
            )

        if editor_mode == "scripted":
            selected_index = fresh_selection
        else:
            emit("Select candidate number (eligible only; blank=discard):")
            try:
                raw_selection = _read_bounded_line(input_stream, _MAX_ACTION_CHARS)
                selected_index = _parse_selection(raw_selection, len(shortlist.candidates))
            except _InputLineTooLong, ValueError:
                emit("SELECTION_REJECTED=INVALID_SELECTION")
                emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
                emit("DRAFT_PERSISTED=NO remote_mutations=0")
                _assert_no_private_persistence(root, api, ())
                return _result(
                    root,
                    accounts,
                    watch,
                    api,
                    shortlist,
                    "DISCARDED",
                    False,
                    len(shortlist.candidates),
                    None,
                )
            except KeyboardInterrupt:
                emit("SELECTION_CANCELLED=YES")
                emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
                emit("DRAFT_PERSISTED=NO remote_mutations=0")
                _assert_no_private_persistence(root, api, ())
                return _result(
                    root,
                    accounts,
                    watch,
                    api,
                    shortlist,
                    "DISCARDED",
                    False,
                    len(shortlist.candidates),
                    None,
                )
            if selected_index is None:
                emit("SELECTION_CANCELLED=YES")
                emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
                emit("DRAFT_PERSISTED=NO remote_mutations=0")
                _assert_no_private_persistence(root, api, ())
                return _result(
                    root,
                    accounts,
                    watch,
                    api,
                    shortlist,
                    "DISCARDED",
                    False,
                    len(shortlist.candidates),
                    None,
                )

        try:
            candidate = _select_candidate(shortlist, selected_index, displayed_candidates)
        except ValueError:
            emit("SELECTION_REJECTED=TARGET_OR_ELIGIBILITY_CHANGED")
            emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
            emit("DRAFT_PERSISTED=NO remote_mutations=0")
            _assert_no_private_persistence(root, api, ())
            return _result(
                root,
                accounts,
                watch,
                api,
                shortlist,
                "DISCARDED",
                False,
                len(shortlist.candidates),
                None,
            )
        selected_label = _candidate_label(candidate)
        if selected_label != "F":
            emit("SELECTION_REJECTED=WATCHED_CANDIDATE_NOT_DRAFTABLE")
            emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
            emit("DRAFT_PERSISTED=NO remote_mutations=0")
            _assert_no_private_persistence(root, api, ())
            return _result(
                root,
                accounts,
                watch,
                api,
                shortlist,
                "DISCARDED",
                False,
                len(shortlist.candidates),
                None,
            )

        generator = _FakeReplyGenerator()
        try:
            preview = asyncio.run(
                create_human_reply_preview(
                    shortlist,
                    selected_index,
                    generator,
                )
            )
        except ReplyProposalError as error:
            emit(f"PROPOSAL=STOP reason={error.code}")
            emit("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING DELIVERY_STATUS=NOT_SENT")
            emit("DRAFT_PERSISTED=NO remote_mutations=0")
            _assert_no_private_persistence(root, api, ())
            return _result(
                root,
                accounts,
                watch,
                api,
                shortlist,
                "DISCARDED",
                False,
                len(shortlist.candidates),
                None,
            )
        if (
            preview.candidate is not candidate
            or preview.proposal.target_fingerprint != candidate.fingerprint
            or not preview.proposal.is_bound_to(candidate)
            or len(generator.requests) != 1
        ):
            raise RuntimeError("synthetic proposal target binding failed")

        emit(f"SELECTED_CANDIDATE=F status={candidate.status} action={candidate.action_hint}")
        emit("PROPOSAL_STATUS=PROPOSED_FOR_HUMAN_REVIEW")
        state = HumanReplyEditorialSession.begin(preview)
        _write_editor_preview(state, output_stream, writes)
        observed_edits: list[str] = []
        if editor_mode == "scripted":
            editor_input = io.StringIO(
                f"E\n{_LOW_INFORMATION_REPLY}\nE\n{_EDITED_REPLIES[_FRESH_TEXT]}\nK\n"
            )
        else:
            editor_input = input_stream
        state = _run_editor(state, editor_input, output_stream, writes, observed_edits)
        emit(f"EDITORIAL_DISPOSITION={state.editorial_disposition}")
        emit(f"APPROVAL_STATUS={state.approval_status}")
        emit(f"DELIVERY_STATUS={state.delivery_status}")
        emit("DRAFT_PERSISTED=NO remote_mutations=0")

        receipt_count, receipt_bytes, state_bytes = _nurture_storage_summary(root, accounts)
        _assert_no_private_persistence(
            root,
            api,
            tuple(_FAKE_REPLIES.values())
            + tuple(_EDITED_REPLIES.values())
            + tuple(observed_edits)
            + (_LOW_INFORMATION_REPLY,),
        )
        if receipt_count != 1 or api.remote_mutations != 0:
            raise RuntimeError("synthetic journey persistence or mutation check failed")
        return JourneyResult(
            "COMPLETED",
            None,
            watch,
            len(shortlist.candidates),
            selected_label,
            state.editorial_disposition,
            True,
            receipt_count,
            receipt_bytes,
            state_bytes,
            api.remote_mutations,
        )
    finally:
        recruitment_module.build_standalone_app = original_builder


def _select_candidate(
    shortlist: NurtureShortlistResult,
    selection: int,
    displayed_candidates: tuple[tuple[NurtureShortlistCandidate, str], ...],
) -> NurtureShortlistCandidate:
    if (
        type(shortlist) is not NurtureShortlistResult
        or type(selection) is not int
        or not 1 <= selection <= len(shortlist.candidates)
        or len(displayed_candidates) != len(shortlist.candidates)
    ):
        raise ValueError("invalid synthetic selection")
    candidate = shortlist.candidates[selection - 1]
    displayed_candidate, displayed_fingerprint = displayed_candidates[selection - 1]
    if (
        type(candidate) is not NurtureShortlistCandidate
        or candidate is not displayed_candidate
        or candidate.fingerprint != displayed_fingerprint
        or candidate.status != "ELIGIBLE"
        or candidate.action_hint != "REVIEW_CANDIDATE"
        or candidate.text is None
        or candidate.text_truncated
    ):
        raise ValueError("synthetic target changed or is not eligible")
    return candidate


def _candidate_label(candidate: NurtureShortlistCandidate) -> Literal["W", "F"]:
    if candidate.text == _WATCHED_TEXT:
        return "W"
    if candidate.text == _FRESH_TEXT:
        return "F"
    raise RuntimeError("unexpected synthetic candidate")


def _write_editor_preview(
    session: HumanReplyEditorialSession,
    output_stream: TextIO,
    writes: list[int],
) -> None:
    context = session.candidate.text
    reply = session.current_reply
    if context is None or reply is None:
        raise RuntimeError("synthetic editor preview unavailable")
    advisory = evaluate_synthetic_reply_quality(session)
    _write_bounded(
        output_stream,
        "SYNTHETIC_CONTEXT="
        + json.dumps(context, ensure_ascii=True)
        + "\nCURRENT_REPLY="
        + json.dumps(reply, ensure_ascii=True)
        + f"\nEDITORIAL_QUALITY={advisory.outcome}"
        + f"\nQUALITY_FLAGS={','.join(advisory.flags) or 'NONE'}\n",
        writes,
    )
    if advisory.flags:
        _write_bounded(
            output_stream,
            "Quality advisory: Add a useful detail before keeping; K remains session-only.\n",
            writes,
        )
    else:
        _write_bounded(
            output_stream,
            "Quality advisory: No obvious issue detected; this does not establish correctness or usefulness.\n",
            writes,
        )


def _run_editor(
    session: HumanReplyEditorialSession,
    input_stream: TextIO,
    output_stream: TextIO,
    writes: list[int],
    observed_edits: list[str],
) -> HumanReplyEditorialSession:
    state = session
    _write_bounded(output_stream, "Action [E=edit, K=keep for session, D=discard]: ", writes)
    edit_count = 0
    while True:
        try:
            action = _read_bounded_line(input_stream, _MAX_ACTION_CHARS)
            _write_bounded(output_stream, "\n", writes)
            if action is None or action == "":
                return state.discard()
            action = action.strip().upper()
            if action == "K":
                return state.keep_for_session()
            if action == "D":
                return state.discard()
            if action != "E":
                _write_bounded(output_stream, "INPUT_REJECTED=INVALID_ACTION\n", writes)
                return state.discard()
            if edit_count >= _MAX_EDITOR_EDITS:
                _write_bounded(output_stream, "EDIT_REJECTED=EDIT_LIMIT_REACHED\n", writes)
                return state.discard()
            _write_bounded(output_stream, "Edited reply (single line, 1-500 characters): ", writes)
            edited = _read_bounded_line(input_stream, _MAX_EDIT_CHARS)
            _write_bounded(output_stream, "\n", writes)
            if edited is None:
                return state.discard()
            observed_edits.append(edited)
            edit_count += 1
            try:
                state = state.edit(edited)
            except ReplyProposalError as error:
                _write_bounded(output_stream, f"EDIT_REJECTED={error.code}\n", writes)
                return state.discard()
            _write_editor_preview(state, output_stream, writes)
            _write_bounded(
                output_stream, "Action [E=edit, K=keep for session, D=discard]: ", writes
            )
        except _InputLineTooLong:
            _write_bounded(output_stream, "INPUT_REJECTED=INPUT_LIMIT_EXCEEDED\n", writes)
            return state.discard()
        except KeyboardInterrupt:
            _write_bounded(output_stream, "INPUT_CANCELLED=YES\n", writes)
            return state.discard()


def _read_bounded_line(stream: TextIO, maximum_characters: int) -> str | None:
    raw = stream.readline(maximum_characters + 3)
    if raw == "":
        return None
    value = raw.removesuffix("\n").removesuffix("\r")
    if len(value) > maximum_characters:
        raise _InputLineTooLong()
    return value


def _parse_selection(raw: str | None, maximum: int) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        selection = int(raw)
    except ValueError:
        raise ValueError("invalid selection") from None
    if not 1 <= selection <= maximum:
        raise ValueError("invalid selection")
    return selection


def _nurture_storage_summary(
    root: Path,
    accounts: LocalAccountStore,
) -> tuple[int, int, int]:
    account = accounts.get("demo")
    completed = root / "nurture" / "runs" / str(account.id) / "completed"
    receipts = tuple(completed.glob("*.json")) if completed.is_dir() else ()
    receipt_bytes = sum(path.stat().st_size for path in receipts)
    state = root / "nurture" / "state" / str(account.id) / "recruitment.json"
    return len(receipts), receipt_bytes, state.stat().st_size if state.is_file() else 0


def _assert_no_private_persistence(
    root: Path,
    api: _SyntheticReadOnlyApi,
    extra_private_values: tuple[str, ...],
) -> None:
    private_values = [
        _WATCHED_TEXT.encode(),
        _FRESH_TEXT.encode(),
        *(reply.encode() for reply in _FAKE_REPLIES.values()),
        *(reply.encode() for reply in _EDITED_REPLIES.values()),
        *(value.encode() for value in extra_private_values),
        b"PROPOSED_FOR_HUMAN_REVIEW",
        b"LOW_INFORMATION",
        b"REVIEW_RECOMMENDED",
        b"KEPT_FOR_SESSION",
        b"DISCARDED",
        b"NOT_APPROVED_FOR_SENDING",
        b"NOT_SENT",
    ]
    if api.remote_mutations != 0:
        raise RuntimeError("synthetic remote mutation check failed")
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            if any(value and value in content for value in private_values):
                raise RuntimeError("synthetic private data persistence check failed")


def _result(
    root: Path,
    accounts: LocalAccountStore,
    watch: WatchRunResult,
    api: _SyntheticReadOnlyApi,
    shortlist: NurtureShortlistResult | None,
    editorial_disposition: str,
    proposal_created: bool,
    shortlist_count: int,
    reason: str | None,
) -> JourneyResult:
    receipt_count, receipt_bytes, state_bytes = _nurture_storage_summary(root, accounts)
    return JourneyResult(
        "STOP" if reason is not None else "COMPLETED",
        reason,
        watch,
        len(shortlist.candidates) if shortlist is not None else shortlist_count,
        None,
        editorial_disposition,
        proposal_created,
        receipt_count,
        receipt_bytes,
        state_bytes,
        api.remote_mutations,
    )


def _write_bounded(output: TextIO, value: str, counter: list[int]) -> None:
    if counter[0] + len(value) > _MAX_OUTPUT_CHARS:
        raise RuntimeError("synthetic output exceeded its limit")
    counter[0] += len(value)
    output.write(value)
    output.flush()


def _private_interactive_terminal_available(
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    environment: Mapping[str, str],
) -> bool:
    if (
        any(
            environment.get(name, "").strip().casefold() in {"1", "true", "yes"}
            for name in ("CI", "GITHUB_ACTIONS")
        )
        or environment.get("PYTEST_CURRENT_TEST", "").strip()
    ):
        return False
    try:
        return all(stream.isatty() for stream in (input_stream, output_stream, error_stream))
    except AttributeError, OSError, ValueError:
        return False


def run_demo(
    *,
    editor_mode: Literal["scripted", "interactive"],
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    environment: Mapping[str, str],
) -> int:
    if editor_mode == "interactive" and not _private_interactive_terminal_available(
        input_stream,
        output_stream,
        error_stream,
        environment,
    ):
        error_stream.write("demo status=STOP reason=PRIVATE_TTY_REQUIRED\n")
        error_stream.flush()
        return 2

    now = datetime.now(UTC)
    api = _SyntheticReadOnlyApi(now)
    timing = _SyntheticTiming(now)
    with tempfile.TemporaryDirectory(prefix="threads-standalone-ao05a-") as temporary_root:
        root = Path(temporary_root).resolve(strict=True)
        accounts = LocalAccountStore(root)
        account = accounts.add("demo")
        if account.credential_ref is not None:
            raise RuntimeError("synthetic account unexpectedly has credentials")
        result = run_journey(
            root,
            accounts,
            api,
            timing,
            editor_mode=editor_mode,
            input_stream=input_stream,
            output_stream=output_stream,
        )
        if result.status != "COMPLETED":
            output_stream.write("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING\n")
            output_stream.write("DELIVERY_STATUS=NOT_SENT\n")
            output_stream.write("DRAFT_PERSISTED=NO\n")
        output_stream.write(
            f"JOURNEY_STATUS={result.status} remote_mutations={api.remote_mutations}\n"
        )
        output_stream.flush()
        return 0 if result.status == "COMPLETED" else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    input_value = input_stream if input_stream is not None else sys.stdin
    output_value = output_stream if output_stream is not None else sys.stdout
    error_value = error_stream if error_stream is not None else sys.stderr
    environment_value = environment if environment is not None else os.environ
    parser = argparse.ArgumentParser(prog="demo_recruitment_journey")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scripted", action="store_true")
    mode.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)
    editor_mode: Literal["scripted", "interactive"] = (
        "interactive" if args.interactive else "scripted"
    )
    if editor_mode == "interactive" and not _private_interactive_terminal_available(
        input_value,
        output_value,
        error_value,
        environment_value,
    ):
        error_value.write("demo status=STOP reason=PRIVATE_TTY_REQUIRED\n")
        error_value.flush()
        return 2
    try:
        return run_demo(
            editor_mode=editor_mode,
            input_stream=input_value,
            output_stream=output_value,
            error_stream=error_value,
            environment=environment_value,
        )
    except KeyboardInterrupt:
        output_value.write("EDITORIAL_DISPOSITION=DISCARDED\n")
        output_value.write("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING\n")
        output_value.write("DELIVERY_STATUS=NOT_SENT\n")
        output_value.write("DRAFT_PERSISTED=NO remote_mutations=0\n")
        output_value.flush()
        return 130
    except Exception:  # noqa: BLE001 — keep synthetic data and paths out of diagnostics.
        error_value.write("JOURNEY_STATUS=STOP reason=SYNTHETIC_DEMO_FAILED\n")
        error_value.flush()
        output_value.write("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING\n")
        output_value.write("DELIVERY_STATUS=NOT_SENT\n")
        output_value.write("DRAFT_PERSISTED=NO remote_mutations=0\n")
        output_value.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
