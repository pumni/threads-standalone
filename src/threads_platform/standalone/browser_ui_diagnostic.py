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
    BrowserRuntimeUnavailable,
)
from threads_platform.browser.ui_diagnostic import UiDiagnosticSnapshot
from threads_platform.standalone.browser_nurture_state import BrowserNurtureConfigV1
from threads_platform.standalone.runtime import (
    LocalRuntime,
    StandaloneRuntimeError,
)

_STAGE_WAIT_SECONDS = 600
_RECOVERY_WAIT_SECONDS = 90
_CLOSE_ACK_WAIT_SECONDS = 60
_RENDER_WAIT_SECONDS = 12
_RENDER_SAMPLE_INTERVAL_SECONDS = 0.35
_STABLE_SAMPLES_REQUIRED = 4
_MAX_STAGE_RETRIES = 2
_HOME_URL = f"{BROWSER_FEED_ORIGIN}/"
_ALLOWED_ORIGINS = frozenset({BROWSER_FEED_ORIGIN})
_STAGE_KEYS = ("home", "search", "detail")
_STAGE_ROUTES = {"home": "HOME", "search": "SEARCH", "detail": "POST"}
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
        "DIAGNOSTIC_FAILED",
        "INTERRUPTED",
        "LOCAL_PROFILE_UNAVAILABLE",
        "NODE_BOUND_EXCEEDED",
        "NONE",
        "OPERATOR_DECISION_TIMEOUT",
        "OPERATOR_SKIPPED",
        "OPERATOR_STOP",
        "REMOTE_STATE_UNCERTAIN",
        "STAGE_WAIT_TIMEOUT",
        "UI_STILL_LOADING",
        "UNSUPPORTED_UI_STATE",
    }
)
_RECOVERABLE_UI_CODES = frozenset(
    {"BROWSER_UI_DIAGNOSTIC_UNAVAILABLE", "NODE_BOUND_EXCEEDED", "UI_STILL_LOADING"}
)

DiagnosticChoice = Literal["OBSERVE", "RETRY", "SKIP", "STOP"]
DiagnosticWaiter = Callable[[str, frozenset[DiagnosticChoice]], Awaitable[DiagnosticChoice]]


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


class _TerminalWaiter(Protocol):
    async def __call__(
        self,
        prompt: str,
        choices: frozenset[DiagnosticChoice],
        *,
        output: TextIO | None = None,
    ) -> DiagnosticChoice: ...


def _safe_reason_code(value: str) -> str:
    return value if value in _SAFE_REASON_CODES else "DIAGNOSTIC_FAILED"


def _choice_for_key(key: str, choices: frozenset[DiagnosticChoice]) -> DiagnosticChoice | None:
    normalized = key.strip().lower()
    if normalized in {"", "enter", "\r", "\n"}:
        return "OBSERVE" if "OBSERVE" in choices else None
    if normalized in {"r", "retry"}:
        return "RETRY" if "RETRY" in choices else None
    if normalized in {"k", "skip"}:
        return "SKIP" if "SKIP" in choices else None
    if normalized in {"s", "stop", "\x03", "\x04", "\x1a"}:
        return "STOP" if "STOP" in choices else None
    return None


async def _wait_for_windows_choice(
    choices: frozenset[DiagnosticChoice],
) -> DiagnosticChoice:
    import msvcrt

    console = cast(_WindowsConsole, msvcrt)
    while True:
        if console.kbhit():
            key = console.getwch()
            if key in {"\x03", "\x1a"}:
                return "STOP"
            choice = _choice_for_key(key, choices)
            if choice is not None:
                return choice
        await asyncio.sleep(0.05)


async def _wait_for_posix_choice(
    choices: frozenset[DiagnosticChoice],
) -> DiagnosticChoice:
    loop = asyncio.get_running_loop()
    input_ready: asyncio.Future[DiagnosticChoice] = loop.create_future()
    try:
        input_fd = sys.stdin.fileno()
    except AttributeError, OSError, ValueError:
        return "STOP"

    def read_console_input() -> None:
        if input_ready.done():
            return
        try:
            characters = os.read(input_fd, 64)
        except OSError:
            input_ready.set_result("STOP")
            return
        if not characters or any(
            character in characters for character in (b"\x03", b"\x04", b"\x1a")
        ):
            input_ready.set_result("STOP")
            return
        line = characters.decode("utf-8", errors="ignore").strip()
        choice = _choice_for_key(line, choices)
        if choice is not None:
            input_ready.set_result(choice)

    try:
        loop.add_reader(input_fd, read_console_input)
    except NotImplementedError, RuntimeError, ValueError:
        return "STOP"
    try:
        return await input_ready
    finally:
        loop.remove_reader(input_fd)


async def wait_for_diagnostic_choice(
    prompt: str,
    choices: frozenset[DiagnosticChoice] = frozenset({"OBSERVE", "STOP"}),
    *,
    output: TextIO | None = None,
) -> DiagnosticChoice:
    """Wait for an operator choice without blocking the Playwright event loop."""

    output_stream = sys.stdout if output is None else output
    output_stream.write(prompt)
    output_stream.flush()
    if os.name == "nt":
        return await _wait_for_windows_choice(choices)
    return await _wait_for_posix_choice(choices)


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


def _render_ready(snapshot: UiDiagnosticSnapshot, stage: str) -> bool:
    if snapshot.status != "OBSERVED" or not snapshot.document_ready:
        return False
    if snapshot.route_class != _STAGE_ROUTES[stage]:
        return False
    counts = dict(snapshot.counts)
    if counts.get("visible_candidates") == "ZERO":
        return False
    return any(
        counts.get(key) in {"ONE", "MANY"}
        for key in ("navigation_landmarks", "main_landmarks", "article_landmarks")
    )


async def wait_for_stable_ui_snapshot(
    session: BrowserUiDiagnosticSession,
    *,
    stage: str,
    expected_username: str,
    configured_topic: str,
) -> UiDiagnosticSnapshot:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RENDER_WAIT_SECONDS
    previous: UiDiagnosticSnapshot | None = None
    stable_samples = 0
    while loop.time() < deadline:
        snapshot = await session.inspect_ui_diagnostic(
            expected_username=expected_username,
            configured_topic=configured_topic,
        )
        if snapshot.status == "NODE_BOUND_EXCEEDED":
            raise BrowserRuntimeUnavailable("NODE_BOUND_EXCEEDED")
        if _render_ready(snapshot, stage):
            if snapshot == previous:
                stable_samples += 1
            else:
                previous = snapshot
                stable_samples = 1
            if stable_samples >= _STABLE_SAMPLES_REQUIRED:
                return snapshot
        else:
            previous = None
            stable_samples = 0
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(min(_RENDER_SAMPLE_INTERVAL_SECONDS, remaining))
    raise BrowserRuntimeUnavailable("UI_STILL_LOADING")


async def _ask(
    waiter: DiagnosticWaiter,
    prompt: str,
    choices: frozenset[DiagnosticChoice],
    *,
    output: TextIO,
    timeout: float,
) -> DiagnosticChoice:
    if waiter is wait_for_diagnostic_choice:
        terminal_waiter = cast(_TerminalWaiter, waiter)
        choice = await asyncio.wait_for(
            terminal_waiter(prompt, choices, output=output),
            timeout=timeout,
        )
    else:
        choice = await asyncio.wait_for(waiter(prompt, choices), timeout=timeout)
    return choice if choice in choices else "STOP"


def _emit(output: TextIO, value: str) -> None:
    output.write(value + "\n")
    output.flush()


def _emit_report(output: TextIO, report: dict[str, object]) -> None:
    output.write(format_browser_ui_diagnostic(report))
    output.flush()


async def run_browser_ui_diagnostic(
    runtime: LocalRuntime,
    alias: str,
    config: BrowserNurtureConfigV1,
    *,
    waiter: DiagnosticWaiter = wait_for_diagnostic_choice,
    output: TextIO | None = None,
) -> dict[str, object]:
    """Observe three operator-selected checkpoints under the existing profile lease."""

    report = _new_report()
    output_stream = sys.stdout if output is None else output
    report_emitted = False
    hard_stop = False
    graceful_stop = False
    skipped = False

    try:
        async with runtime.own_browser_profile(alias, headless=False) as lease:
            try:
                await lease.start()
                session = lease.session
                if session is None:
                    raise BrowserRuntimeUnavailable("BROWSER_START_FAILED")
                diagnostic_session = cast(BrowserUiDiagnosticSession, session)
                await diagnostic_session.navigate(_HOME_URL, allowed_origins=_ALLOWED_ORIGINS)

                for stage in _STAGE_KEYS:
                    _emit(
                        output_stream,
                        f"DIAGNOSTIC_STAGE stage={stage.upper()} status=WAITING_FOR_OPERATOR",
                    )
                    try:
                        choice = await _ask(
                            waiter,
                            _STAGE_PROMPTS[stage],
                            frozenset({"OBSERVE", "STOP"}),
                            output=output_stream,
                            timeout=_STAGE_WAIT_SECONDS,
                        )
                    except TimeoutError:
                        report["status"] = "STOPPED"
                        report["reason_code"] = "STAGE_WAIT_TIMEOUT"
                        graceful_stop = True
                        break
                    if choice == "STOP":
                        report["status"] = "STOPPED"
                        report["reason_code"] = "OPERATOR_STOP"
                        graceful_stop = True
                        break

                    attempts = 0
                    while True:
                        _emit(
                            output_stream,
                            f"DIAGNOSTIC_STAGE stage={stage.upper()} status=WAITING_FOR_RENDER",
                        )
                        try:
                            snapshot = await wait_for_stable_ui_snapshot(
                                diagnostic_session,
                                stage=stage,
                                expected_username=config.expected_username,
                                configured_topic=config.topics[0],
                            )
                        except BrowserAdapterError as error:
                            code = _safe_reason_code(error.code)
                            _emit(
                                output_stream, f"DIAGNOSTIC_ERROR stage={stage.upper()} code={code}"
                            )
                            _set_stage(report, stage, {"status": "ERROR", "reason_code": code})
                            if code not in _RECOVERABLE_UI_CODES:
                                report["status"] = "ERROR"
                                report["reason_code"] = code
                                hard_stop = True
                                break

                            if attempts < _MAX_STAGE_RETRIES:
                                choices: frozenset[DiagnosticChoice] = frozenset(
                                    {"RETRY", "SKIP", "STOP"}
                                )
                            else:
                                choices = frozenset({"SKIP", "STOP"})
                            options = ",".join(sorted(choices))
                            input_hint = (
                                "Press R/K/S.\n"
                                if os.name == "nt"
                                else "Type R/K/S, then press Enter.\n"
                            )
                            _emit(
                                output_stream,
                                f"DIAGNOSTIC_RECOVERY stage={stage.upper()} code={code} "
                                f"attempt={attempts + 1} options={options}",
                            )
                            try:
                                recovery = await _ask(
                                    waiter,
                                    f"Recoverable diagnostic issue {code}. "
                                    f"Choose {options}. {input_hint}",
                                    choices,
                                    output=output_stream,
                                    timeout=_RECOVERY_WAIT_SECONDS,
                                )
                            except TimeoutError:
                                report["status"] = "STOPPED"
                                report["reason_code"] = "OPERATOR_DECISION_TIMEOUT"
                                graceful_stop = True
                                break
                            if recovery == "RETRY":
                                attempts += 1
                                continue
                            if recovery == "SKIP":
                                _set_stage(
                                    report,
                                    stage,
                                    {"status": "SKIPPED", "reason_code": code},
                                )
                                skipped = True
                                break
                            report["status"] = "STOPPED"
                            report["reason_code"] = "OPERATOR_STOP"
                            graceful_stop = True
                            break

                        _set_stage(
                            report,
                            stage,
                            {**snapshot.to_document(), "stability": "STABLE"},
                        )
                        break

                    if hard_stop or graceful_stop:
                        break

                if not hard_stop and not graceful_stop:
                    report["status"] = "PARTIAL" if skipped else "COMPLETE"
                    report["reason_code"] = "OPERATOR_SKIPPED" if skipped else "NONE"
            except KeyboardInterrupt, asyncio.CancelledError:
                report["status"] = "STOPPED"
                report["reason_code"] = "INTERRUPTED"
                hard_stop = True
            except BrowserAdapterError as error:
                code = _safe_reason_code(error.code)
                report["status"] = "ERROR"
                report["reason_code"] = code
                _emit(output_stream, f"DIAGNOSTIC_ERROR stage=HOME code={code}")
                hard_stop = True
            except StandaloneRuntimeError as error:
                code = _safe_reason_code(error.code)
                report["status"] = "ERROR"
                report["reason_code"] = code
                _emit(output_stream, f"DIAGNOSTIC_ERROR stage=HOME code={code}")
                hard_stop = True
            except Exception:  # noqa: BLE001 - browser details never cross the report boundary.
                report["status"] = "ERROR"
                report["reason_code"] = "DIAGNOSTIC_FAILED"
                _emit(output_stream, "DIAGNOSTIC_ERROR stage=HOME code=DIAGNOSTIC_FAILED")
                hard_stop = True

            _emit_report(output_stream, report)
            report_emitted = True
            if not hard_stop:
                _emit(
                    output_stream,
                    f"DIAGNOSTIC_CLOSE_WAIT timeout_seconds={_CLOSE_ACK_WAIT_SECONDS}",
                )
                try:
                    close_choice = await _ask(
                        waiter,
                        "Diagnostic report is ready. Press Enter to close Chromium, or S to stop it now.\n",
                        frozenset({"OBSERVE", "STOP"}),
                        output=output_stream,
                        timeout=_CLOSE_ACK_WAIT_SECONDS,
                    )
                except TimeoutError:
                    _emit(output_stream, "DIAGNOSTIC_CLOSE code=CLOSE_ACK_TIMEOUT")
                else:
                    close_code = (
                        "OPERATOR_CONFIRMED" if close_choice == "OBSERVE" else "OPERATOR_STOP"
                    )
                    _emit(output_stream, f"DIAGNOSTIC_CLOSE code={close_code}")
    except BrowserAdapterError as error:
        code = _safe_reason_code(error.code)
        report["status"] = "ERROR"
        report["reason_code"] = code
        _emit(output_stream, f"DIAGNOSTIC_ERROR stage=HOME code={code}")
    except StandaloneRuntimeError as error:
        code = _safe_reason_code(error.code)
        report["status"] = "ERROR"
        report["reason_code"] = code
        _emit(output_stream, f"DIAGNOSTIC_ERROR stage=HOME code={code}")
    except KeyboardInterrupt, asyncio.CancelledError:
        report["status"] = "STOPPED"
        report["reason_code"] = "INTERRUPTED"
    except Exception:  # noqa: BLE001 - fixed safe output at the CLI boundary.
        report["status"] = "ERROR"
        report["reason_code"] = "DIAGNOSTIC_FAILED"
        _emit(output_stream, "DIAGNOSTIC_ERROR stage=HOME code=DIAGNOSTIC_FAILED")

    if not report_emitted:
        _emit_report(output_stream, report)
    return report


def format_browser_ui_diagnostic(report: dict[str, object]) -> str:
    """Serialize only snapshots already reduced to the fixed symbolic schema."""

    return json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
