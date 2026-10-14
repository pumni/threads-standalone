"""One supervised, read-only Home → Search → Detail semantic observation."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TextIO, cast

from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
)
from threads_platform.browser.ui_diagnostic import UiDiagnosticSnapshot
from threads_platform.standalone.browser_nurture_state import BrowserNurtureConfigV1
from threads_platform.standalone.runtime import (
    LocalRuntime,
    StandaloneRuntimeError,
)

_STAGE_WAIT_SECONDS = 600
_HOME_URL = f"{BROWSER_FEED_ORIGIN}/"
_ALLOWED_ORIGINS = frozenset({BROWSER_FEED_ORIGIN})
_STAGE_KEYS = ("home", "search", "detail")
_STAGE_PROMPTS = {
    "home": (
        "Home checkpoint: inspect the loaded Home page in Chromium. "
        "Press Enter to record it, or S to stop.\n"
    ),
    "search": (
        "Search checkpoint: manually open Search, submit the first configured topic, "
        "and wait for visible results. Press Enter to record it, or S to stop.\n"
    ),
    "detail": (
        "Detail checkpoint: manually open one suitable visible post. "
        "Press Enter to record it, or S to stop if none is available.\n"
    ),
}
_SAFE_REASON_CODES = frozenset(
    {
        "ACCOUNT_BUSY",
        "BROWSER_BINARY_MISSING",
        "BROWSER_NAVIGATION_FAILED",
        "BROWSER_NAVIGATION_TIMEOUT",
        "BROWSER_PAGE_CREATION_FAILED",
        "BROWSER_PROCESS_CRASHED",
        "BROWSER_RUNTIME_UNAVAILABLE",
        "BROWSER_START_FAILED",
        "BROWSER_UI_DIAGNOSTIC_SCHEMA_INVALID",
        "BROWSER_UI_DIAGNOSTIC_UNAVAILABLE",
        "LOCAL_PROFILE_UNAVAILABLE",
        "REMOTE_STATE_UNCERTAIN",
        "UNSUPPORTED_UI_STATE",
    }
)

DiagnosticChoice = Literal["OBSERVE", "STOP"]
DiagnosticWaiter = Callable[[str], Awaitable[DiagnosticChoice]]


class BrowserUiDiagnosticSession(Protocol):
    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None: ...

    async def inspect_ui_diagnostic(
        self,
        *,
        expected_username: str,
        configured_topic: str,
    ) -> UiDiagnosticSnapshot: ...


class _WindowsConsole(Protocol):
    def kbhit(self) -> bool: ...

    def getwch(self) -> str: ...


def _safe_reason_code(value: str) -> str:
    return value if value in _SAFE_REASON_CODES else "DIAGNOSTIC_FAILED"


async def _wait_for_windows_choice() -> DiagnosticChoice:
    import msvcrt

    console = cast(_WindowsConsole, msvcrt)
    while True:
        if console.kbhit():
            key = console.getwch()
            if key in {"\r", "\n"}:
                return "OBSERVE"
            if key.lower() == "s" or key in {"\x03", "\x1a"}:
                return "STOP"
        await asyncio.sleep(0.05)


async def _wait_for_posix_choice() -> DiagnosticChoice:
    loop = asyncio.get_running_loop()
    input_ready: asyncio.Future[DiagnosticChoice] = loop.create_future()
    input_fd = sys.stdin.fileno()

    def read_console_input() -> None:
        if input_ready.done():
            return
        try:
            character = os.read(input_fd, 1)
        except OSError:
            input_ready.set_result("STOP")
            return
        if character in {b"", b"\x04", b"\x1a", b"\x03"}:
            input_ready.set_result("STOP")
        elif character in {b"\r", b"\n"}:
            input_ready.set_result("OBSERVE")
        elif character.lower() == b"s":
            input_ready.set_result("STOP")

    loop.add_reader(input_fd, read_console_input)
    try:
        return await input_ready
    finally:
        loop.remove_reader(input_fd)


async def wait_for_diagnostic_choice(
    prompt: str,
    *,
    output: TextIO | None = None,
) -> DiagnosticChoice:
    """Wait for one operator checkpoint without blocking the Playwright event loop."""

    output_stream = sys.stdout if output is None else output
    output_stream.write(prompt)
    output_stream.flush()
    if os.name == "nt":
        return await _wait_for_windows_choice()
    return await _wait_for_posix_choice()


def _new_report() -> dict[str, object]:
    return {
        "schema": "THREADS_UI_DIAGNOSTIC_V1",
        "status": "STOPPED",
        "reason_code": "NOT_STARTED",
        "stages": {stage: {"status": "NOT_OBSERVED"} for stage in _STAGE_KEYS},
    }


def _set_stage(
    report: dict[str, object],
    stage: str,
    value: dict[str, object],
) -> None:
    stages = cast(dict[str, dict[str, object]], report["stages"])
    stages[stage] = value


async def run_browser_ui_diagnostic(
    runtime: LocalRuntime,
    alias: str,
    config: BrowserNurtureConfigV1,
    *,
    waiter: DiagnosticWaiter = wait_for_diagnostic_choice,
) -> dict[str, object]:
    """Observe three operator-selected checkpoints under the existing profile lease."""

    report = _new_report()
    bounded_stage = False
    try:
        async with runtime.own_browser_profile(alias, headless=False) as lease:
            await lease.start()
            session = lease.session
            if session is None:
                report["status"] = "ERROR"
                report["reason_code"] = "BROWSER_START_FAILED"
                return report
            diagnostic_session = cast(BrowserUiDiagnosticSession, session)
            await diagnostic_session.navigate(_HOME_URL, allowed_origins=_ALLOWED_ORIGINS)
            for stage in _STAGE_KEYS:
                try:
                    choice = await asyncio.wait_for(
                        waiter(_STAGE_PROMPTS[stage]),
                        timeout=_STAGE_WAIT_SECONDS,
                    )
                except TimeoutError:
                    report["status"] = "STOPPED"
                    report["reason_code"] = "STAGE_WAIT_TIMEOUT"
                    return report
                if choice == "STOP":
                    report["status"] = "STOPPED"
                    report["reason_code"] = "OPERATOR_STOP"
                    return report
                try:
                    snapshot = await diagnostic_session.inspect_ui_diagnostic(
                        expected_username=config.expected_username,
                        configured_topic=config.topics[0],
                    )
                except BrowserAdapterError as error:
                    _set_stage(
                        report,
                        stage,
                        {"status": "ERROR", "reason_code": _safe_reason_code(error.code)},
                    )
                    report["status"] = "INCOMPLETE"
                    report["reason_code"] = _safe_reason_code(error.code)
                    return report
                _set_stage(report, stage, snapshot.to_document())
                bounded_stage = bounded_stage or snapshot.status == "NODE_BOUND_EXCEEDED"
    except BrowserAdapterError as error:
        report["status"] = "ERROR"
        report["reason_code"] = _safe_reason_code(error.code)
        return report
    except StandaloneRuntimeError as error:
        report["status"] = "ERROR"
        report["reason_code"] = _safe_reason_code(error.code)
        return report
    except KeyboardInterrupt, asyncio.CancelledError:
        report["status"] = "STOPPED"
        report["reason_code"] = "INTERRUPTED"
        return report
    except TimeoutError:
        report["status"] = "ERROR"
        report["reason_code"] = "STAGE_WAIT_TIMEOUT"
        return report
    except Exception:  # noqa: BLE001 - browser details never cross the report boundary.
        report["status"] = "ERROR"
        report["reason_code"] = "DIAGNOSTIC_FAILED"
        return report
    report["status"] = "PARTIAL" if bounded_stage else "COMPLETE"
    report["reason_code"] = "NODE_BOUND_EXCEEDED" if bounded_stage else "NONE"
    return report


def format_browser_ui_diagnostic(report: dict[str, object]) -> str:
    """Serialize only snapshots already reduced to the fixed symbolic schema."""

    return json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
