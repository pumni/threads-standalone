"""Run the recruitment shortlist against a fake-only API and a fresh temporary root."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncGenerator, Callable
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
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_runner import NurtureShortlistResult
from threads_platform.standalone.nurture_store import (
    NurtureStore,
    fingerprint_remote_thread,
)
from threads_platform.standalone.recruitment_session import RecruitmentSession

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_LIMIT = 5
_GOOD = RemoteDiscoveryThread(
    "ao03_synthetic_good_001",
    "ao03_synthetic_author_001",
    "synthetic_author_one",
    "Synthetic career question? How should I prepare for an interview?",
    None,
    None,
    _NOW - timedelta(hours=1),
    False,
    False,
)
_COOLDOWN = RemoteDiscoveryThread(
    "ao03_synthetic_cooldown_002",
    "ao03_synthetic_author_002",
    "synthetic_author_two",
    "Synthetic career planning opportunity for a new role.",
    None,
    None,
    _NOW - timedelta(hours=2),
    False,
    True,
)
_EXCLUDED = RemoteDiscoveryThread(
    "ao03_synthetic_excluded_003",
    "ao03_synthetic_author_003",
    "synthetic_author_three",
    "Synthetic crypto promotion about a career opportunity?",
    None,
    None,
    _NOW - timedelta(hours=3),
    False,
    True,
)


class _SyntheticApiRuntime:
    """Read-only fake; deliberately has no mutation methods or credential resolver."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        self.calls.append("identity")
        return RemoteAuthenticatedProfile("ao03_synthetic_owner_id", "synthetic_owner")

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and limit > 0
        self.calls.append("mentions")
        return DiscoveryPage((_GOOD,), None, False)

    async def search(
        self,
        _alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert query and after is None and limit > 0
        assert search_mode in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
        assert search_type is DiscoverySearchType.RECENT
        self.calls.append("search")
        return DiscoveryPage((_COOLDOWN, _EXCLUDED) if len(self.calls) == 2 else (), None, False)

    async def profile_posts(
        self,
        _alias: str,
        _username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert after is None and limit > 0
        self.calls.append("profile_posts")
        return DiscoveryPage((), None, False)

    async def conversation(self, _alias: str, _thread_id: str, after: str | None) -> ReplyPage:
        assert after is None
        self.calls.append("conversation")
        return ReplyPage((), None, False)


def _synthetic_builder(
    expected_root: Path,
    expected_accounts: LocalAccountStore,
    api: _SyntheticApiRuntime,
) -> Callable[..., object]:
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
            raise RuntimeError("synthetic demo permits read-only fake API only")
        if accounts.get("demo").credential_ref is not None:
            raise RuntimeError("synthetic demo account must not contain credentials")
        yield StandaloneAppContext(
            accounts,
            cast(LocalThreadsApiRuntime, api),
            None,
        )

    return build


def _seed_cooldown(
    root: Path,
    account: LocalAccount,
    preset: NurturePresetV1,
) -> None:
    store = NurtureStore(root)
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW - timedelta(minutes=2))
        with scope:
            owner.observe_target(
                preset,
                fingerprint_remote_thread(_COOLDOWN.remote_thread_id),
                scope.receipt.id,
                "DEMO_SYNTHETIC_OBSERVATION",
                now=_NOW - timedelta(minutes=1),
            )
            scope.finish("SUCCESS", now=_NOW - timedelta(minutes=1))


def _format_demo_summary(result: NurtureShortlistResult) -> tuple[str, int]:
    lines = [
        "RECRUITMENT SHORTLIST — SYNTHETIC DEMO",
        f"Inbound: {result.inbound_status.value}",
    ]
    for index, candidate in enumerate(result.candidates, start=1):
        lines.extend(
            (
                (
                    f"{index}. [{candidate.source}] Score "
                    f"{candidate.score if candidate.score is not None else '—'}"
                ),
                f"   Reason: {', '.join(candidate.reason_codes) or 'none'}",
                f"   Status: {candidate.status} — {candidate.action_hint}",
            )
        )
    return "\n".join(lines), len(result.candidates)


def run_demo(
    selection: int = 1,
    reporter: Callable[[str], None] | None = None,
) -> None:
    """Run the production discovery/ranking path against fake data only."""

    if type(selection) is not int or not 1 <= selection <= _LIMIT:
        raise ValueError("selection must be from 1 to 5")
    emit = reporter if reporter is not None else _stdout
    preset = get_nurture_preset("recruitment")
    api = _SyntheticApiRuntime()
    with tempfile.TemporaryDirectory(prefix="threads-standalone-ao03-") as temporary_root:
        root = Path(temporary_root).resolve(strict=True)
        accounts = LocalAccountStore(root)
        account = accounts.add("demo")
        if account.credential_ref is not None:
            raise RuntimeError("synthetic demo account unexpectedly has credentials")
        _seed_cooldown(root, account, preset)
        session = RecruitmentSession(root, accounts)
        original_builder = recruitment_module.build_standalone_app
        recruitment_module.build_standalone_app = _synthetic_builder(root, accounts, api)  # type: ignore[assignment]
        try:
            result = asyncio.run(session.shortlist("demo", now=_NOW, limit=_LIMIT))
        finally:
            recruitment_module.build_standalone_app = original_builder

        summary, count = _format_demo_summary(result)
        if count < selection:
            raise RuntimeError("synthetic demo did not create the requested candidate")
        candidate = result.candidates[selection - 1]
        content = (
            "<unavailable>"
            if candidate.text is None
            else json.dumps(
                candidate.text,
                ensure_ascii=False,
            )
        )
        if candidate.text_truncated:
            content += " … [truncated]"
        emit(summary)
        emit(f"Synthetic selection: {selection}")
        emit(
            "SYNTHETIC CANDIDATE PREVIEW\n"
            f"Source: {candidate.source}\n"
            f"Score: {candidate.score if candidate.score is not None else '—'}\n"
            f"Reason: {', '.join(candidate.reason_codes) or 'none'}\n"
            f"Status: {candidate.status}\n"
            f"Suggested next step: {candidate.action_hint}\n"
            f"Content: {content}\n"
            "Preview only; no approval or remote action was created."
        )
        emit(
            "demo safety backend=FAKE identity=FAKE credential_source=NONE "
            "mutation_runtime=DISABLED remote_mutations=0"
        )


def _parse_selection(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("selection must be from 1 to 5") from None
    if not 1 <= value <= _LIMIT:
        raise argparse.ArgumentTypeError("selection must be from 1 to 5")
    return value


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_recruitment_shortlist")
    parser.add_argument("--select", type=_parse_selection, default=1)
    args = parser.parse_args(argv)
    try:
        run_demo(args.select)
    except KeyboardInterrupt:
        _stdout("demo status=INTERRUPTED")
        return 130
    except Exception:  # noqa: BLE001 — synthetic demo never echoes local paths or data.
        sys.stderr.write("demo status=STOP reason=SYNTHETIC_DEMO_FAILED\n")
        sys.stderr.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
