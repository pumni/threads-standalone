from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import AsyncGenerator
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
    DeterministicFakeReplyGenerator,
    create_human_reply_preview,
)
from threads_platform.standalone.recruitment_session import RecruitmentSession

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_SYNTHETIC_THREAD = RemoteDiscoveryThread(
    "ao04a_synthetic_thread_001",
    "ao04a_synthetic_author_001",
    "synthetic_author",
    "Synthetic career question? What skills help someone prepare for an interview?",
    None,
    None,
    _NOW - timedelta(hours=1),
    False,
    False,
)


class _FakeApi:
    """Read-only fake with no mutation API or credential resolver."""

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        return RemoteAuthenticatedProfile("ao04a_synthetic_owner_id", "synthetic_owner")

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and limit > 0
        return DiscoveryPage((_SYNTHETIC_THREAD,), None, False)

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
        return DiscoveryPage((), None, False)

    async def conversation(self, _alias: str, _thread_id: str, after: str | None) -> ReplyPage:
        assert after is None
        return ReplyPage((), None, False)


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


def run_demo() -> None:
    """Run the existing shortlist policy and an offline fake generator in a fresh root."""

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

        if not shortlist.candidates:
            raise RuntimeError("synthetic demo found no candidate")
        candidate = shortlist.candidates[0]
        preview = asyncio.run(
            create_human_reply_preview(
                shortlist,
                1,
                DeterministicFakeReplyGenerator(),
            )
        )
        if preview.candidate is not candidate:
            raise RuntimeError("synthetic demo candidate binding failed")

        proposal = preview.proposal
        print("AO-04A SYNTHETIC REPLY PROPOSAL")
        print(f"Candidate selected: 1 [{candidate.source}]")
        print(f"Score: {candidate.score if candidate.score is not None else '—'}")
        print(f"Reason codes: {', '.join(candidate.reason_codes) or 'none'}")
        print(f"Candidate status: {candidate.status} — {candidate.action_hint}")
        print(f"Synthetic reply preview: {proposal.suggested_reply}")
        print(f"Proposal status: {proposal.status}")
        print(f"Approval status: {proposal.approval_status}")
        print(f"Delivery status: {proposal.delivery_status}")
        print("Disposition: DISCARDED")
        print("demo backend=FAKE_API generator=FAKE_SYNTHETIC credentials=NONE remote_mutations=0")


def main() -> int:
    try:
        run_demo()
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
