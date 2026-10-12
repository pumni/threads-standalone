"""Explicit S4 handoff and operator-attested status commands."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol, TextIO
from uuid import UUID

from threads_platform.browser.capabilities import BrowserTargetOpenResultV1
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.manual_opportunities import (
    ManualDraftReview,
    ManualOpportunity,
    ManualOpportunityError,
    build_manual_draft_review,
    load_manual_opportunities,
    load_operator_context,
    load_operator_draft,
    private_review_terminal_available,
)
from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeError,
    ManualOutcomeStore,
    ManualOutcomeValue,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class ManualWorkflowError(Exception):
    """Bounded error code for explicit S4 terminal workflows."""

    def __init__(self, code: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("manual workflow error code must be bounded and safe")
        self.code = code
        super().__init__(code)


class InteractiveThreadRuntime(Protocol):
    async def open_thread_interactive(
        self,
        alias: str,
        thread_ref: str,
        *,
        wait_for_operator: Callable[[str], Awaitable[None]] | None = None,
        operator_output: TextIO | None = None,
        expected_account_id: UUID | None = None,
    ) -> BrowserTargetOpenResultV1: ...


RuntimeFactory = Callable[[Path, LocalAccountStore], InteractiveThreadRuntime]


def _require_private_terminal(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
) -> None:
    if not private_review_terminal_available(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environment=environment,
    ):
        raise ManualWorkflowError("PRIVATE_REVIEW_TERMINAL_REQUIRED")


def _selected_opportunity(
    account_store: LocalAccountStore,
    alias: str,
    targets_file: Path,
    selection: int,
    confirm_target: str,
) -> tuple[LocalAccount, ManualOpportunity]:
    account = account_store.get(alias)
    intake = load_manual_opportunities(targets_file)
    if type(selection) is not int or not 1 <= selection <= len(intake.candidates):
        raise ManualOpportunityError("MANUAL_SELECTION_INVALID")
    opportunity = intake.candidates[selection - 1]
    if confirm_target != opportunity.target_url:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    return account, opportunity


def _approved_review(
    account_store: LocalAccountStore,
    alias: str,
    targets_file: Path,
    selection: int,
    confirm_target: str,
    draft_file: Path,
    context_file: Path | None,
    approve_sha: str,
) -> tuple[LocalAccount, ManualDraftReview]:
    account, opportunity = _selected_opportunity(
        account_store,
        alias,
        targets_file,
        selection,
        confirm_target,
    )
    draft = load_operator_draft(draft_file)
    context = load_operator_context(context_file)
    review = build_manual_draft_review(account, opportunity, draft, context)
    if _SHA256.fullmatch(approve_sha) is None:
        raise ManualWorkflowError("MANUAL_APPROVAL_INVALID")
    if not hmac.compare_digest(approve_sha, review.draft_sha256):
        raise ManualWorkflowError("MANUAL_APPROVAL_MISMATCH")
    return account, review


def _read_literal_confirmation(
    stdin: TextIO,
    stderr: TextIO,
    *,
    prompt: str,
    expected: str,
    failure_code: str,
    interrupted_code: str,
) -> None:
    stderr.write(prompt)
    stderr.flush()
    try:
        response = stdin.readline(len(expected) + 8)
    except KeyboardInterrupt:
        raise ManualWorkflowError(interrupted_code) from None
    except EOFError, OSError:
        raise ManualWorkflowError(failure_code) from None
    if response not in {f"{expected}\n", f"{expected}\r\n"}:
        raise ManualWorkflowError(failure_code)


def _render_handoff_preview(account: LocalAccount, review: ManualDraftReview) -> str:
    return (
        "\n".join(
            (
                "MANUAL BROWSER HANDOFF — PRIVATE LOCAL TERMINAL",
                f"account alias: {account.alias}",
                f"account UUID: {account.id}",
                "actor identity: UNKNOWN_IDENTITY; operator must verify the logged-in account",
                f"exact target: {review.opportunity.target_url}",
                f"permalink author: {review.opportunity.author_username} (identity unverified)",
                f"context provenance: {review.context_provenance}",
                "context completeness: UNKNOWN",
                "operator-authored final draft: " + json.dumps(review.draft, ensure_ascii=False),
                f"approved draft SHA-256: {review.draft_sha256}",
                "status before handoff: NOT_SENT",
                "The browser will not fill a reply or click Send. Verify actor and target yourself.",
                "This handoff does not authorize a live action or establish platform permission.",
            )
        )
        + "\n"
    )


def _default_runtime_factory(root: Path, accounts: LocalAccountStore) -> InteractiveThreadRuntime:
    from threads_platform.standalone.runtime import LocalRuntime

    return LocalRuntime(root, accounts)


def _run_s1_handoff(
    runtime: InteractiveThreadRuntime,
    alias: str,
    relative_thread_ref: str,
    expected_account_id: UUID,
    output: TextIO,
) -> BrowserTargetOpenResultV1:
    try:
        return asyncio.run(
            runtime.open_thread_interactive(
                alias,
                relative_thread_ref,
                operator_output=output,
                expected_account_id=expected_account_id,
            )
        )
    except KeyboardInterrupt:
        raise ManualWorkflowError("MANUAL_HANDOFF_INTERRUPTED") from None
    except ManualWorkflowError:
        raise
    except Exception as error:  # noqa: BLE001 — runtime errors expose only their safe code.
        code = getattr(error, "code", None)
        if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) is not None:
            raise ManualWorkflowError(code) from None
        raise ManualWorkflowError("MANUAL_HANDOFF_FAILED") from None


def run_manual_handoff_cli(
    alias: str,
    targets_file: Path,
    selection: int,
    confirm_target: str,
    draft_file: Path,
    approve_sha: str,
    context_file: Path | None,
    *,
    data_root_factory: Callable[[], Path],
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Open only the exact S3-approved target after one literal OPEN confirmation."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    _require_private_terminal(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    )

    root = data_root_factory()
    accounts = LocalAccountStore(root)
    account, review = _approved_review(
        accounts,
        alias,
        targets_file,
        selection,
        confirm_target,
        draft_file,
        context_file,
        approve_sha,
    )
    outcome_store = ManualOutcomeStore(root)
    previous = outcome_store.read(account.id, review.opportunity.target_url)
    if previous is not None and previous.blocks_handoff:
        raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")

    private_stderr.write(_render_handoff_preview(account, review))
    private_stderr.flush()
    _read_literal_confirmation(
        private_stdin,
        private_stderr,
        prompt="Type OPEN and press Enter to open this one target: ",
        expected="OPEN",
        failure_code="MANUAL_HANDOFF_NOT_CONFIRMED",
        interrupted_code="MANUAL_HANDOFF_INTERRUPTED",
    )

    with outcome_store.account_lock(account.id):
        previous = outcome_store.read(account.id, review.opportunity.target_url)
        if previous is not None and previous.blocks_handoff:
            raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
        runtime = runtime_factory(root, accounts)
        result = _run_s1_handoff(
            runtime,
            alias,
            review.relative_thread_ref,
            account.id,
            private_stderr,
        )
        if not result.recognized or result.target_ref != review.relative_thread_ref:
            raise ManualWorkflowError("MANUAL_HANDOFF_RESULT_MISMATCH")
        private_stderr.write(
            "HANDOFF_CLOSED_ACTION_UNVERIFIED "
            f"target={result.target_ref} identity=UNKNOWN_IDENTITY\n"
        )
        private_stderr.flush()
    return 0


def run_manual_record_cli(
    alias: str,
    targets_file: Path,
    selection: int,
    confirm_target: str,
    draft_file: Path,
    approve_sha: str,
    outcome: ManualOutcomeValue,
    context_file: Path | None,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Record only an explicitly confirmed operator declaration, always unverified."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    _require_private_terminal(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    )

    root = data_root_factory()
    accounts = LocalAccountStore(root)
    account, review = _approved_review(
        accounts,
        alias,
        targets_file,
        selection,
        confirm_target,
        draft_file,
        context_file,
        approve_sha,
    )
    outcome_store = ManualOutcomeStore(root)
    fingerprint = outcome_store.target_fingerprint(account.id, review.opportunity.target_url)
    private_stderr.write(
        "MANUAL OUTCOME ATTESTATION — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {account.alias}\n"
        f"account UUID: {account.id}\n"
        f"exact target: {review.opportunity.target_url}\n"
        f"target fingerprint: {fingerprint}\n"
        f"draft SHA-256: {review.draft_sha256}\n"
        f"operator declaration: {outcome}\n"
        "verification: UNVERIFIED; this is not an official receipt or proof of sending\n"
    )
    _read_literal_confirmation(
        private_stdin,
        private_stderr,
        prompt="Type RECORD and press Enter to record this operator declaration: ",
        expected="RECORD",
        failure_code="MANUAL_RECORD_NOT_CONFIRMED",
        interrupted_code="MANUAL_RECORD_INTERRUPTED",
    )
    event, idempotent = outcome_store.record(
        account.id,
        review.opportunity.target_url,
        review.draft_sha256,
        outcome,
    )
    private_stderr.write(
        f"recorded={event.operator_declaration} verification={event.verification} "
        f"event_id={event.event_id} idempotent={str(idempotent).lower()}\n"
    )
    private_stderr.flush()
    return 0


def run_manual_status_cli(
    alias: str,
    targets_file: Path,
    selection: int,
    confirm_target: str,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Read the separate journal and report only a bounded private TTY status."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    _require_private_terminal(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    )

    root = data_root_factory()
    accounts = LocalAccountStore(root)
    account, opportunity = _selected_opportunity(
        accounts,
        alias,
        targets_file,
        selection,
        confirm_target,
    )
    outcome_store = ManualOutcomeStore(root)
    fingerprint = outcome_store.target_fingerprint(account.id, opportunity.target_url)
    record = outcome_store.read(account.id, opportunity.target_url)
    private_stderr.write(
        "MANUAL RECRUITMENT STATUS — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {account.alias}\n"
        f"account UUID: {account.id}\n"
        f"target fingerprint: {fingerprint}\n"
    )
    if record is None:
        private_stderr.write("status=NO_RECORD verification=UNVERIFIED\n")
    else:
        latest = record.latest
        private_stderr.write(
            f"status={latest.operator_declaration} verification={latest.verification} "
            f"event_id={latest.event_id} recorded_at={latest.recorded_at} "
            f"draft_sha256={latest.draft_sha256}\n"
        )
    private_stderr.flush()
    return 0
