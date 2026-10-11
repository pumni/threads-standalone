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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO, cast

from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
    ReplyPage,
)
from threads_platform.standalone import recruitment_session as recruitment_module
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.recruitment_reply_proposal import (
    HumanReplyEditorialSession,
    HumanReplyPreview,
    ReplyProposalError,
    ReplyProposalInput,
    create_human_reply_preview,
)
from threads_platform.standalone.recruitment_session import RecruitmentSession

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_MAX_PREVIEW_CHARS = 500
_MAX_OUTPUT_CHARS = 2_048
_SYNTHETIC_THREADS = (
    RemoteDiscoveryThread(
        "ao04a_synthetic_thread_a_001",
        "ao04a_synthetic_author_a_001",
        "synthetic_author_a",
        "SYNTHETIC TARGET A: A recruiter hiring a junior analyst asks what career portfolio helps before an interview?",
        None,
        None,
        _NOW - timedelta(hours=1),
        False,
        False,
    ),
    RemoteDiscoveryThread(
        "ao04a_synthetic_thread_b_002",
        "ao04a_synthetic_author_b_002",
        "synthetic_author_b",
        "SYNTHETIC TARGET B: Career question? What should a product designer show in a case study?",
        None,
        None,
        _NOW - timedelta(hours=2),
        False,
        False,
    ),
)
_REPLIES_BY_CONTEXT = {
    _SYNTHETIC_THREADS[0].text: (
        "For the analyst role, which tools matter most: SQL, spreadsheets, or visualization?"
    ),
    _SYNTHETIC_THREADS[1].text: (
        "For the design role, which part of the product process should applicants emphasize?"
    ),
}
_EDITED_REPLIES_BY_CONTEXT = {
    _SYNTHETIC_THREADS[0].text: (
        "For the analyst role, should candidates highlight a project using SQL or dashboards?"
    ),
    _SYNTHETIC_THREADS[1].text: (
        "For the design role, should a case study focus on research, iterations, or outcomes?"
    ),
}
_MAX_EDITOR_EDITS = 5
_MAX_ACTION_CHARS = 16
_MAX_EDIT_CHARS = 500


class _InputLineTooLong(Exception):
    """A bounded terminal line exceeded its limit; no input is retained in the error."""


class _FakeApi:
    """Read-only fake with no mutation API or credential resolver."""

    def __init__(self) -> None:
        self.read_calls = 0
        self.remote_mutations = 0

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        self.read_calls += 1
        return RemoteAuthenticatedProfile("ao04a_synthetic_owner_id", "synthetic_owner")

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and limit > 0
        self.read_calls += 1
        return DiscoveryPage(_SYNTHETIC_THREADS, None, False)

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
        assert search_mode in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
        assert search_type is DiscoverySearchType.RECENT and after is None and limit > 0
        self.read_calls += 1
        return DiscoveryPage((), None, False)

    async def profile_posts(
        self,
        _alias: str,
        _username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and limit > 0
        self.read_calls += 1
        return DiscoveryPage((), None, False)

    async def conversation(self, _alias: str, _thread_id: str, after: str | None) -> ReplyPage:
        assert after is None
        self.read_calls += 1
        return ReplyPage((), None, False)


class _FakeReplyGenerator:
    """Deterministic content-to-reply mapping with no model, client, or network path."""

    async def generate_reply(self, request: ReplyProposalInput) -> str:
        try:
            return _REPLIES_BY_CONTEXT[request.candidate_text]
        except KeyError:
            raise RuntimeError("unknown synthetic candidate") from None


def _fake_builder(
    expected_root: Path,
    expected_accounts: LocalAccountStore,
    api: _FakeApi,
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
            or app_root.resolve() != expected_root
            or not include_api
            or include_mutations
            or include_browser
        ):
            raise RuntimeError("synthetic demo permits read-only fake API only")
        if accounts.get("demo").credential_ref is not None:
            raise RuntimeError("synthetic demo account must not contain credentials")
        yield StandaloneAppContext(
            accounts,
            cast(LocalThreadsApiRuntime, api),
            None,
        )

    return build


def run_demo(
    selection: int = 1,
    *,
    editor_mode: Literal["legacy", "interactive", "scripted"] = "legacy",
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Run existing shortlist policy and a fake generator in a fresh synthetic root."""

    if type(selection) is not int or not 1 <= selection <= len(_SYNTHETIC_THREADS):
        raise ValueError("invalid synthetic candidate selection")
    output = output_stream if output_stream is not None else sys.stdout
    error = error_stream if error_stream is not None else sys.stderr
    environment_values = environment if environment is not None else os.environ
    if editor_mode == "interactive" and not _private_interactive_terminal_available(
        input_stream if input_stream is not None else sys.stdin,
        output,
        error,
        environment_values,
    ):
        error.write("demo status=STOP reason=PRIVATE_TTY_REQUIRED\n")
        error.flush()
        raise _PrivateTerminalRequired()
    if editor_mode not in {"legacy", "interactive", "scripted"}:
        raise ValueError("invalid synthetic demo mode")

    api = _FakeApi()
    with tempfile.TemporaryDirectory(prefix="threads-standalone-ao04a-") as temporary_root:
        root = Path(temporary_root).resolve(strict=True)
        accounts = LocalAccountStore(root)
        account = accounts.add("demo")
        if account.credential_ref is not None:
            raise RuntimeError("synthetic demo account unexpectedly has credentials")

        session = RecruitmentSession(root, accounts)
        original_builder = recruitment_module.build_standalone_app
        recruitment_module.build_standalone_app = _fake_builder(root, accounts, api)  # type: ignore[assignment]
        try:
            shortlist = asyncio.run(session.shortlist("demo", now=_NOW, limit=5))
        finally:
            recruitment_module.build_standalone_app = original_builder

        if len(shortlist.candidates) != len(_SYNTHETIC_THREADS):
            raise RuntimeError("synthetic demo shortlist was incomplete")
        candidate = shortlist.candidates[selection - 1]
        preview = asyncio.run(
            create_human_reply_preview(
                shortlist,
                selection,
                _FakeReplyGenerator(),
            )
        )
        if preview.candidate is not candidate:
            raise RuntimeError("synthetic demo candidate binding failed")
        if (
            candidate.text is None
            or candidate.text_truncated
            or len(candidate.text) > _MAX_PREVIEW_CHARS
            or len(preview.proposal.suggested_reply) > _MAX_PREVIEW_CHARS
            or api.read_calls == 0
            or api.remote_mutations != 0
        ):
            raise RuntimeError("synthetic demo bounds or effect check failed")

        proposal = preview.proposal
        candidate_values = tuple(
            (
                item.text,
                item.fingerprint,
                next(
                    thread.remote_thread_id
                    for thread in _SYNTHETIC_THREADS
                    if thread.text == item.text
                ),
            )
            for item in shortlist.candidates
            if item.text is not None
        )
        if editor_mode != "legacy":
            editor_input = input_stream if input_stream is not None else sys.stdin
            observed_edits: list[str] = []
            if editor_mode == "scripted":
                edited_reply = _EDITED_REPLIES_BY_CONTEXT[candidate.text or ""]
                editor_input = io.StringIO(f"E\n{edited_reply}\nK\n")
            output.write("AO-04B SYNTHETIC HUMAN REPLY EDITOR\n")
            output.write(f"Candidate selected: {selection} [{candidate.source}]\n")
            state = _run_editor_session(
                preview,
                editor_input,
                output,
                observed_edits=observed_edits,
            )
            _assert_no_proposal_persistence(
                root,
                candidate_values=candidate_values,
                reply_texts=(
                    *tuple(_REPLIES_BY_CONTEXT.values()),
                    *tuple(_EDITED_REPLIES_BY_CONTEXT.values()),
                    *tuple(observed_edits),
                ),
            )
            if api.remote_mutations != 0:
                raise RuntimeError("synthetic editor mutation check failed")
            _write_editor_outcome(state, output)
            return

        _assert_no_proposal_persistence(
            root,
            candidate_values=candidate_values,
            reply_texts=tuple(_REPLIES_BY_CONTEXT.values()),
        )
        lines = (
            "AO-04A SYNTHETIC REPLY PROPOSAL",
            f"Candidate selected: {selection} [{candidate.source}]",
            f"Score: {candidate.score if candidate.score is not None else '—'}",
            f"Reason codes: {', '.join(candidate.reason_codes) or 'none'}",
            f"Candidate status: {candidate.status} — {candidate.action_hint}",
            f"Synthetic candidate context: {json.dumps(candidate.text, ensure_ascii=True)}",
            f"Fake reply proposal: {proposal.suggested_reply}",
            f"Proposal status: {proposal.status}",
            f"Approval status: {proposal.approval_status}",
            f"Delivery status: {proposal.delivery_status}",
            "Disposition: DISCARDED",
            (
                "demo backend=FAKE_API generator=FAKE_SYNTHETIC credentials=NONE "
                "candidate_or_reply_persistence=0 remote_mutations=0"
            ),
        )
        rendered = "\n".join(lines)
        if len(rendered) > _MAX_OUTPUT_CHARS:
            raise RuntimeError("synthetic demo output exceeded its bound")
        output.write(rendered + "\n")
        output.flush()


class _PrivateTerminalRequired(Exception):
    """Safe control signal used after refusing an unsafe interactive invocation."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *, prog: str, error_stream: TextIO) -> None:
        self._safe_error_stream = error_stream
        super().__init__(prog=prog)

    def error(self, message: str) -> None:
        self._print_message(self.format_usage(), self._safe_error_stream)
        safe_message = (
            "selection must be 1 or 2"
            if message.endswith("selection must be 1 or 2")
            else "INVALID_ARGUMENT"
        )
        self._print_message(f"{self.prog}: error: {safe_message}\n", self._safe_error_stream)
        raise SystemExit(2)


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


def _read_bounded_line(stream: TextIO, maximum_characters: int) -> str | None:
    raw = stream.readline(maximum_characters + 3)
    if raw == "":
        return None
    value = raw.removesuffix("\n").removesuffix("\r")
    if len(value) > maximum_characters:
        raise _InputLineTooLong()
    return value


def _write_editor_preview(
    session: HumanReplyEditorialSession,
    output_stream: TextIO,
) -> None:
    context = session.candidate.text
    reply = session.current_reply
    if context is None or reply is None:
        raise RuntimeError("synthetic editor preview unavailable")
    output_stream.write("SYNTHETIC CONTEXT | CURRENT FAKE / EDITED REPLY\n")
    output_stream.write(
        f"{json.dumps(context, ensure_ascii=True)} | {json.dumps(reply, ensure_ascii=True)}\n"
    )
    output_stream.flush()


def _write_editor_outcome(
    session: HumanReplyEditorialSession,
    output_stream: TextIO,
) -> None:
    if session.editorial_disposition == "EDITING":
        raise RuntimeError("synthetic editor did not finish")
    output_stream.write(f"EDITORIAL_DISPOSITION={session.editorial_disposition}\n")
    output_stream.write(f"APPROVAL_STATUS={session.approval_status}\n")
    output_stream.write(f"DELIVERY_STATUS={session.delivery_status}\n")
    output_stream.write("DRAFT_PERSISTED=NO remote_mutations=0\n")
    output_stream.flush()


def _run_editor_session(
    preview: HumanReplyPreview,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    observed_edits: list[str],
) -> HumanReplyEditorialSession:
    state = HumanReplyEditorialSession.begin(preview)
    _write_editor_preview(state, output_stream)
    edit_count = 0
    while True:
        try:
            output_stream.write("Action [E=edit, K=keep for session, D=discard]: ")
            output_stream.flush()
            action = _read_bounded_line(input_stream, _MAX_ACTION_CHARS)
            output_stream.write("\n")
            if action is None or action == "":
                state = state.discard()
                break
            action = action.strip().upper()
            if action == "K":
                state = state.keep_for_session()
                break
            if action == "D":
                state = state.discard()
                break
            if action != "E":
                output_stream.write("INPUT_REJECTED=INVALID_ACTION\n")
                state = state.discard()
                break
            if edit_count >= _MAX_EDITOR_EDITS:
                output_stream.write("EDIT_REJECTED=EDIT_LIMIT_REACHED\n")
                state = state.discard()
                break
            output_stream.write("Edited reply (single line, 1-500 characters): ")
            output_stream.flush()
            edited = _read_bounded_line(input_stream, _MAX_EDIT_CHARS)
            output_stream.write("\n")
            if edited is None:
                state = state.discard()
                break
            if edited:
                observed_edits.append(edited)
            edit_count += 1
            try:
                state = state.edit(edited)
            except ReplyProposalError as error:
                output_stream.write(f"EDIT_REJECTED={error.code}\n")
                state = state.discard()
                break
            _write_editor_preview(state, output_stream)
        except _InputLineTooLong:
            output_stream.write("INPUT_REJECTED=INPUT_LIMIT_EXCEEDED\n")
            state = state.discard()
            break
        except KeyboardInterrupt:
            output_stream.write("INPUT_CANCELLED=YES\n")
            state = state.discard()
            break
    return state


def _assert_no_proposal_persistence(
    root: Path,
    *,
    candidate_values: tuple[tuple[str, str, str], ...],
    reply_texts: tuple[str, ...],
) -> None:
    forbidden = tuple(
        value.encode("utf-8", errors="surrogatepass")
        for candidate_text, fingerprint, remote_id in candidate_values
        for value in (candidate_text, fingerprint, remote_id)
    ) + tuple(reply.encode("utf-8", errors="surrogatepass") for reply in reply_texts)
    try:
        for path in root.rglob("*"):
            if path.is_file():
                persisted = path.read_bytes()
                if any(value in persisted for value in forbidden):
                    raise RuntimeError("synthetic candidate or reply was persisted")
    except OSError:
        raise RuntimeError("synthetic demo persistence check failed") from None


def _parse_selection(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("selection must be 1 or 2") from None
    if not 1 <= value <= len(_SYNTHETIC_THREADS):
        raise argparse.ArgumentTypeError("selection must be 1 or 2")
    return value


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
    parser = _SafeArgumentParser(
        prog="demo_recruitment_ai_draft",
        error_stream=error_value,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--interactive", action="store_true")
    mode_group.add_argument("--scripted", action="store_true")
    parser.add_argument(
        "--select",
        type=_parse_selection,
        default=1,
        metavar="N",
        help="choose synthetic shortlist candidate 1 or 2 (default: 1)",
    )
    args = parser.parse_args(argv)
    if args.interactive and not _private_interactive_terminal_available(
        input_value,
        output_value,
        error_value,
        environment_value,
    ):
        error_value.write("demo status=STOP reason=PRIVATE_TTY_REQUIRED\n")
        error_value.flush()
        return 2
    try:
        run_demo(
            args.select,
            editor_mode="interactive"
            if args.interactive
            else "scripted"
            if args.scripted
            else "legacy",
            input_stream=input_value,
            output_stream=output_value,
            error_stream=error_value,
            environment=environment_value,
        )
    except KeyboardInterrupt:
        if args.interactive:
            output_value.write("EDITORIAL_DISPOSITION=DISCARDED\n")
            output_value.write("APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING\n")
            output_value.write("DELIVERY_STATUS=NOT_SENT\n")
            output_value.write("DRAFT_PERSISTED=NO remote_mutations=0\n")
        else:
            error_value.write("demo status=INTERRUPTED\n")
        output_value.flush()
        return 130
    except _PrivateTerminalRequired:
        return 2
    except Exception:  # noqa: BLE001 — never print synthetic local state or exception details.
        error_value.write("demo status=STOP reason=SYNTHETIC_DEMO_FAILED\n")
        error_value.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
