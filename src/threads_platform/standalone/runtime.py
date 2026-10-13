"""Manual browser lifecycle for standalone local accounts."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, TextIO, cast
from uuid import UUID

from threads_platform.browser.capabilities import (
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserContractError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserFeedEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserProfileOpenEngineSession,
    BrowserThreadOpenEngineSession,
    RemoteSessionStateUncertain,
)
from threads_platform.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.browser.semantics import (
    BROWSER_FEED_CANDIDATE_BOUND,
    BROWSER_FEED_ITERATION_BOUND,
    BROWSER_FEED_URL,
    BROWSER_READ_TARGET_ANCESTOR_BOUND,
    normalize_feed_permalinks,
    normalize_profile_username,
    parse_thread_ref,
)
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_BROWSER_READ_TIMEOUT_SECONDS = 30
_THREADS_WEB_ORIGINS = frozenset({BROWSER_FEED_ORIGIN})
_DEFAULT_OPERATOR_WAITER = cast(Callable[[str], None], input)


async def _wait_for_windows_console_enter(
    key_available: Callable[[], bool],
    read_key: Callable[[], str],
) -> None:
    while True:
        if key_available():
            key = read_key()
            if key in {"\r", "\n"}:
                return
            if key == "\x1a":
                raise EOFError
            if key == "\x03":
                raise KeyboardInterrupt
        await asyncio.sleep(0.05)


async def _wait_for_interactive_operator(
    prompt: str,
    *,
    output: TextIO | None = None,
) -> None:
    output_stream = sys.stdout if output is None else output
    output_stream.write(prompt)
    output_stream.flush()
    if not sys.stdin.isatty():
        raise EOFError

    if os.name == "nt":
        import msvcrt

        await _wait_for_windows_console_enter(msvcrt.kbhit, msvcrt.getwch)
    else:
        loop = asyncio.get_running_loop()
        input_ready: asyncio.Future[None] = loop.create_future()
        input_fd = sys.stdin.fileno()

        def read_console_input() -> None:
            if input_ready.done():
                return
            try:
                character = os.read(input_fd, 1)
            except OSError:
                input_ready.set_exception(EOFError())
                return
            if character in {b"", b"\x04"}:
                input_ready.set_exception(EOFError())
            elif character in {b"\r", b"\n"}:
                input_ready.set_result(None)

        loop.add_reader(input_fd, read_console_input)
        try:
            await input_ready
        finally:
            loop.remove_reader(input_fd)

    output_stream.write("\n")
    output_stream.flush()


_DEFAULT_INTERACTIVE_OPERATOR_WAITER = _wait_for_interactive_operator


class StandaloneRuntimeError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("standalone runtime error code must be bounded and safe")
        self.code = code
        super().__init__(code)


class LocalRuntime:
    def __init__(
        self,
        data_root: Path,
        accounts: LocalAccountStore,
        browser_engine: BrowserEngine | None = None,
    ) -> None:
        self._data_root = data_root
        self._accounts = accounts
        self._browser_engine = (
            browser_engine if browser_engine is not None else PlaywrightBrowserEngine()
        )

    async def login(
        self,
        alias: str,
        *,
        wait_for_operator: Callable[[str], None] = _DEFAULT_OPERATOR_WAITER,
    ) -> None:
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(self._lock_path(account))
        try:
            try:
                lock.acquire()
            except ProcessAlreadyRunning:
                raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
            except OSError:
                raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

            profile_directory = self._profile_directory(account)
            try:
                profile_directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
            profile_directory = self._profile_directory(account)

            request = BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                proxy_credentials=None,
                headless=False,
            )

            session: BrowserEngineSession | None = None
            try:
                session = await self._browser_engine.open(request)
                prompt = (
                    f"Browser opened for {alias}. Log in to Threads manually, complete any normal "
                    "challenge, then press Enter here to close the browser: "
                )
                wait_for_operator(prompt)
            finally:
                if session is not None:
                    await session.close()
        except BrowserAdapterError as error:
            raise StandaloneRuntimeError(error.code) from None
        finally:
            lock.release()

    async def open_profile(self, alias: str, username: str) -> BrowserTargetOpenResultV1:
        target_ref = normalize_profile_username(username)
        if target_ref is None:
            raise StandaloneRuntimeError("INVALID_PROFILE_TARGET")
        return await self._open_browser_target(alias, target_ref, target_kind="PROFILE")

    async def open_thread(self, alias: str, thread_ref: str) -> BrowserTargetOpenResultV1:
        parsed_target = parse_thread_ref(thread_ref)
        if parsed_target is None:
            raise StandaloneRuntimeError("INVALID_THREAD_TARGET")
        target_ref, author_username = parsed_target
        return await self._open_browser_target(
            alias,
            target_ref,
            target_kind="THREAD",
            author_username=author_username,
        )

    async def open_thread_interactive(
        self,
        alias: str,
        thread_ref: str,
        *,
        wait_for_operator: Callable[[str], Awaitable[None]] | None = None,
        operator_output: TextIO | None = None,
        expected_account_id: UUID | None = None,
    ) -> BrowserTargetOpenResultV1:
        parsed_target = parse_thread_ref(thread_ref)
        if parsed_target is None:
            raise StandaloneRuntimeError("INVALID_THREAD_TARGET")
        target_ref, author_username = parsed_target
        operator_waiter: Callable[[str], Awaitable[None]]
        if wait_for_operator is not None:
            operator_waiter = wait_for_operator
        elif operator_output is not None:

            async def wait_on_operator_output(prompt: str) -> None:
                await _wait_for_interactive_operator(prompt, output=operator_output)

            operator_waiter = wait_on_operator_output

        else:
            operator_waiter = _DEFAULT_INTERACTIVE_OPERATOR_WAITER
        return await self._open_browser_target(
            alias,
            target_ref,
            target_kind="THREAD",
            author_username=author_username,
            wait_for_operator=operator_waiter,
            expected_account_id=expected_account_id,
        )

    async def browse_feed(self, alias: str, max_items: int = 10) -> BrowserFeedResultV1:
        if type(max_items) is not int or not 1 <= max_items <= 20:
            raise StandaloneRuntimeError("INVALID_FEED_LIMIT")
        try:
            async with asyncio.timeout(_BROWSER_READ_TIMEOUT_SECONDS):
                account = await asyncio.to_thread(self._accounts.get, alias)
                lock = FilesystemProcessLock(self._lock_path(account))
                try:
                    lock.acquire()
                except ProcessAlreadyRunning:
                    raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
                except OSError:
                    raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

                session: BrowserEngineSession | None = None
                try:
                    profile_directory = self._profile_directory(account)
                    if not profile_directory.is_dir():
                        raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE")
                    request = BrowserLaunchRequest(
                        profile_directory=profile_directory,
                        network_route=BrowserNetworkRoute(
                            BrowserNetworkProtocol.DIRECT,
                            None,
                            None,
                        ),
                        proxy_credentials=None,
                        headless=False,
                    )
                    session = await self._browser_engine.open(request)
                    feed_session = cast(BrowserFeedEngineSession, session)
                    await feed_session.navigate(
                        BROWSER_FEED_URL,
                        allowed_origins=_THREADS_WEB_ORIGINS,
                    )

                    observations: list[BrowserFeedItemResultV1] = []
                    seen_refs: set[str] = set()
                    truncated = False
                    for iteration in range(BROWSER_FEED_ITERATION_BOUND):
                        if iteration:
                            await feed_session.scroll_feed()
                        permalinks = await feed_session.collect_feed_permalinks(
                            candidate_bound=BROWSER_FEED_CANDIDATE_BOUND
                        )
                        batch = normalize_feed_permalinks(
                            permalinks,
                            max_items=max_items - len(observations),
                            existing_refs=frozenset(seen_refs),
                            position_start=len(observations),
                        )
                        for item in batch:
                            if item.thread_ref is None:
                                raise BrowserContractError()
                            seen_refs.add(item.thread_ref)
                            observations.append(item)
                        if len(observations) >= max_items:
                            truncated = True
                            break
                        if not batch:
                            if iteration == BROWSER_FEED_ITERATION_BOUND - 1:
                                if observations:
                                    truncated = True
                                    break
                                raise RemoteSessionStateUncertain()
                            continue
                        if iteration == BROWSER_FEED_ITERATION_BOUND - 1:
                            truncated = True

                    return BrowserFeedResultV1(
                        observations=tuple(observations[:max_items]),
                        truncated=truncated,
                    )
                except BrowserAdapterError as error:
                    raise StandaloneRuntimeError(error.code) from None
                except StandaloneRuntimeError:
                    raise
                except Exception:  # noqa: BLE001 - convert unexpected browser failures to safe CLI codes
                    raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
                finally:
                    try:
                        if session is not None:
                            await session.close()
                    finally:
                        try:
                            lock.release()
                        except OSError:
                            raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
        except TimeoutError:
            raise StandaloneRuntimeError("BROWSER_NAVIGATION_TIMEOUT") from None

    async def _open_browser_target(
        self,
        alias: str,
        target_ref: str,
        *,
        target_kind: Literal["PROFILE", "THREAD"],
        author_username: str | None = None,
        wait_for_operator: Callable[[str], Awaitable[None]] | None = None,
        expected_account_id: UUID | None = None,
    ) -> BrowserTargetOpenResultV1:
        lock: FilesystemProcessLock | None = None
        session: BrowserEngineSession | None = None
        try:
            async with asyncio.timeout(_BROWSER_READ_TIMEOUT_SECONDS):
                account = await asyncio.to_thread(self._accounts.get, alias)
                if expected_account_id is not None and account.id != expected_account_id:
                    raise StandaloneRuntimeError("ACCOUNT_IDENTITY_CHANGED")
                lock = FilesystemProcessLock(self._lock_path(account))
                try:
                    lock.acquire()
                except ProcessAlreadyRunning:
                    raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
                except OSError:
                    raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

                try:
                    profile_directory = self._profile_directory(account)
                    if not profile_directory.is_dir():
                        raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE")
                    request = BrowserLaunchRequest(
                        profile_directory=profile_directory,
                        network_route=BrowserNetworkRoute(
                            BrowserNetworkProtocol.DIRECT,
                            None,
                            None,
                        ),
                        proxy_credentials=None,
                        headless=False,
                    )
                    session = await self._browser_engine.open(request)
                    await session.navigate(
                        f"{BROWSER_FEED_ORIGIN}{target_ref}",
                        allowed_origins=_THREADS_WEB_ORIGINS,
                    )
                    if target_kind == "PROFILE":
                        profile_session = cast(BrowserProfileOpenEngineSession, session)
                        await profile_session.verify_profile_target(
                            target_ref=target_ref,
                            ancestor_bound=BROWSER_READ_TARGET_ANCESTOR_BOUND,
                        )
                    else:
                        if author_username is None:
                            raise StandaloneRuntimeError("BROWSER_CONTRACT_MISMATCH")
                        thread_session = cast(BrowserThreadOpenEngineSession, session)
                        await thread_session.verify_thread_target(
                            target_ref=target_ref,
                            author_username=author_username,
                        )
                except BrowserAdapterError as error:
                    raise StandaloneRuntimeError(error.code) from None
                except StandaloneRuntimeError:
                    raise
                except Exception:  # noqa: BLE001 - convert unexpected browser failures to safe CLI codes
                    raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None

            result = BrowserTargetOpenResultV1(
                target_kind=target_kind,
                target_ref=target_ref,
                recognized=True,
            )
            if wait_for_operator is not None:
                prompt = (
                    f"Thread target recognized target={target_ref} author=@{author_username}; "
                    f"account={alias} identity=UNKNOWN_IDENTITY. Verify the logged-in account "
                    "yourself. Press Enter here to end and close this browser session: "
                )
                try:
                    await wait_for_operator(prompt)
                except EOFError:
                    raise StandaloneRuntimeError("OPERATOR_HANDOFF_ABORTED") from None
                except Exception:  # noqa: BLE001 - keep interactive input errors bounded
                    raise StandaloneRuntimeError("OPERATOR_HANDOFF_FAILED") from None
                if author_username is None:
                    raise StandaloneRuntimeError("BROWSER_CONTRACT_MISMATCH")
                try:
                    await cast(BrowserThreadOpenEngineSession, session).verify_thread_target(
                        target_ref=target_ref,
                        author_username=author_username,
                    )
                except BrowserAdapterError:
                    raise
                except Exception:  # noqa: BLE001 - keep browser failures as safe CLI codes
                    raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
            return result
        except TimeoutError:
            raise StandaloneRuntimeError("BROWSER_NAVIGATION_TIMEOUT") from None
        except BrowserAdapterError as error:
            raise StandaloneRuntimeError(error.code) from None
        finally:
            try:
                if session is not None:
                    await session.close()
            finally:
                if lock is not None and lock.held:
                    try:
                        lock.release()
                    except OSError:
                        raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

    def _profile_directory(self, account: LocalAccount) -> Path:
        return self._resolve_managed_path("profiles", str(account.id))

    def _lock_path(self, account: LocalAccount) -> Path:
        return self._resolve_managed_path("locks", f"{account.id}.lock")

    def _resolve_managed_path(self, *parts: str) -> Path:
        try:
            root = self._data_root.resolve(strict=False)
            path = root.joinpath(*parts).resolve(strict=False)
            path.relative_to(root)
        except OSError, RuntimeError, ValueError:
            raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
        return path
