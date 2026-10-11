from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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


def run_demo(selection: int = 1) -> None:
    """Run existing shortlist policy and a fake generator in a fresh synthetic root."""

    if type(selection) is not int or not 1 <= selection <= len(_SYNTHETIC_THREADS):
        raise ValueError("invalid synthetic candidate selection")
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
        _assert_no_proposal_persistence(
            root,
            candidate_values=tuple(
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
            ),
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
        output = "\n".join(lines)
        if len(output) > _MAX_OUTPUT_CHARS:
            raise RuntimeError("synthetic demo output exceeded its bound")
        sys.stdout.write(output + "\n")
        sys.stdout.flush()


def _assert_no_proposal_persistence(
    root: Path,
    *,
    candidate_values: tuple[tuple[str, str, str], ...],
    reply_texts: tuple[str, ...],
) -> None:
    forbidden = tuple(
        value.encode("utf-8")
        for candidate_text, fingerprint, remote_id in candidate_values
        for value in (candidate_text, fingerprint, remote_id)
    ) + tuple(reply.encode("utf-8") for reply in reply_texts)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_recruitment_ai_draft")
    parser.add_argument(
        "--select",
        type=_parse_selection,
        default=1,
        metavar="N",
        help="choose synthetic shortlist candidate 1 or 2 (default: 1)",
    )
    args = parser.parse_args(argv)
    try:
        run_demo(args.select)
    except KeyboardInterrupt:
        print("demo status=INTERRUPTED")
        return 130
    except Exception:  # noqa: BLE001 — never print synthetic local state or exception details.
        sys.stderr.write("demo status=STOP reason=SYNTHETIC_DEMO_FAILED\n")
        sys.stderr.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
