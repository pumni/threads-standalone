"""Offline, private review of operator supplied Threads opportunities."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlsplit

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import normalize_manual_permalink
from threads_platform.browser.semantics import parse_thread_ref
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_MAX_TARGET_FILE_BYTES = 64 * 1024
_MAX_TARGET_INPUTS = 20
_MAX_URL_BYTES = 2048
_MAX_DRAFT_CHARS = 500
_MAX_CONTEXT_FILE_BYTES = 16 * 1024
_MAX_CONTEXT_CHARS = 4000
_CI_VARIABLES = ("CI", "GITHUB_ACTIONS")


class ManualOpportunityError(Exception):
    """Safe error containing a fixed code only."""

    _CODES = frozenset(
        {
            "PRIVATE_REVIEW_TERMINAL_REQUIRED",
            "MANUAL_TARGET_FILE_INVALID",
            "MANUAL_TARGET_FILE_TOO_LARGE",
            "MANUAL_TARGET_ENCODING_INVALID",
            "MANUAL_TARGET_COUNT_INVALID",
            "MANUAL_TARGET_URL_INVALID",
            "MANUAL_DRAFT_FILE_INVALID",
            "MANUAL_DRAFT_ENCODING_INVALID",
            "MANUAL_DRAFT_CONTENT_INVALID",
            "MANUAL_CONTEXT_FILE_INVALID",
            "MANUAL_CONTEXT_ENCODING_INVALID",
            "MANUAL_CONTEXT_CONTENT_INVALID",
            "MANUAL_SELECTION_INVALID",
            "MANUAL_TARGET_MISMATCH",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported manual opportunity error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ManualOpportunity:
    input_position: int
    source_line: int
    target_url: str
    author_username: str


@dataclass(frozen=True, slots=True, repr=False)
class DuplicateOpportunity:
    source_line: int
    shortlist_index: int


@dataclass(frozen=True, slots=True, repr=False)
class ManualOpportunityIntake:
    candidates: tuple[ManualOpportunity, ...]
    duplicates: tuple[DuplicateOpportunity, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ManualDraftReview:
    account_alias: str
    account_id: str
    opportunity: ManualOpportunity
    relative_thread_ref: str
    context: str | None
    context_provenance: str
    context_completeness: Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
    draft: str
    draft_sha256: str


def private_review_terminal_available(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
) -> bool:
    """Return whether the current invocation can show a private review safely."""

    if any(environment.get(name, "").strip() for name in _CI_VARIABLES):
        return False
    try:
        return stdin.isatty() and stdout.isatty() and stderr.isatty()
    except Exception:  # noqa: BLE001 — an uncertain stream must fail closed.
        return False


def _read_bounded_utf8_file(
    path: Path,
    *,
    byte_limit: int,
    invalid_code: str,
    oversized_code: str,
    encoding_code: str,
) -> str:
    try:
        target = Path(path)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ManualOpportunityError(invalid_code)
        if info.st_size > byte_limit:
            raise ManualOpportunityError(oversized_code)
        with target.open("rb") as source:
            content = source.read(byte_limit + 1)
    except ManualOpportunityError:
        raise
    except OSError, TypeError, ValueError:
        raise ManualOpportunityError(invalid_code) from None

    if len(content) > byte_limit:
        raise ManualOpportunityError(oversized_code)
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ManualOpportunityError(encoding_code) from None
    if decoded.startswith("\ufeff"):
        raise ManualOpportunityError(encoding_code)
    return decoded


def _has_unsafe_character(value: str, *, allow_newline: bool = False) -> bool:
    for character in value:
        if allow_newline and character == "\n":
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            return True
        if category == "Zs" and character != " ":
            return True
    return False


def _normalize_operator_text(value: str, *, code: str) -> str:
    normalized = value.replace("\r\n", "\n")
    if "\r" in normalized or _has_unsafe_character(normalized, allow_newline=True):
        raise ManualOpportunityError(code)
    normalized = normalized.strip()
    if not normalized:
        raise ManualOpportunityError(code)
    return normalized


def load_manual_opportunities(path: Path) -> ManualOpportunityIntake:
    """Read, validate, normalize and stably deduplicate one explicit URL file."""

    decoded = _read_bounded_utf8_file(
        path,
        byte_limit=_MAX_TARGET_FILE_BYTES,
        invalid_code="MANUAL_TARGET_FILE_INVALID",
        oversized_code="MANUAL_TARGET_FILE_TOO_LARGE",
        encoding_code="MANUAL_TARGET_ENCODING_INVALID",
    )
    if "\r" in decoded.replace("\r\n", ""):
        raise ManualOpportunityError("MANUAL_TARGET_FILE_INVALID")

    input_lines = [
        (line_number, raw_line.strip(" \t"))
        for line_number, raw_line in enumerate(decoded.replace("\r\n", "\n").split("\n"), start=1)
        if raw_line.strip(" \t")
    ]
    if not 1 <= len(input_lines) <= _MAX_TARGET_INPUTS:
        raise ManualOpportunityError("MANUAL_TARGET_COUNT_INVALID")

    candidates: list[ManualOpportunity] = []
    duplicates: list[DuplicateOpportunity] = []
    seen: dict[str, int] = {}
    for input_position, (source_line, value) in enumerate(input_lines, start=1):
        if (
            len(value) > _MAX_URL_BYTES
            or len(value.encode("utf-8")) > _MAX_URL_BYTES
            or _has_unsafe_character(value)
        ):
            raise ManualOpportunityError("MANUAL_TARGET_URL_INVALID")
        try:
            normalized = normalize_manual_permalink(value)
        except BrowserContractError:
            raise ManualOpportunityError("MANUAL_TARGET_URL_INVALID") from None

        target_key = normalized.thread_ref
        prior_index = seen.get(target_key)
        if prior_index is not None:
            duplicates.append(
                DuplicateOpportunity(source_line=source_line, shortlist_index=prior_index)
            )
            continue

        shortlist_index = len(candidates) + 1
        seen[target_key] = shortlist_index
        candidates.append(
            ManualOpportunity(
                input_position=input_position,
                source_line=source_line,
                target_url=normalized.thread_ref,
                author_username=normalized.author_username,
            )
        )

    if not candidates:
        raise ManualOpportunityError("MANUAL_TARGET_COUNT_INVALID")
    return ManualOpportunityIntake(tuple(candidates), tuple(duplicates))


def load_operator_draft(path: Path) -> str:
    decoded = _read_bounded_utf8_file(
        path,
        byte_limit=_MAX_DRAFT_CHARS * 4,
        invalid_code="MANUAL_DRAFT_FILE_INVALID",
        oversized_code="MANUAL_DRAFT_CONTENT_INVALID",
        encoding_code="MANUAL_DRAFT_ENCODING_INVALID",
    )
    draft = _normalize_operator_text(decoded, code="MANUAL_DRAFT_CONTENT_INVALID")
    if len(draft) > _MAX_DRAFT_CHARS:
        raise ManualOpportunityError("MANUAL_DRAFT_CONTENT_INVALID")
    return draft


def load_operator_context(path: Path | None) -> str | None:
    if path is None:
        return None
    decoded = _read_bounded_utf8_file(
        path,
        byte_limit=_MAX_CONTEXT_FILE_BYTES,
        invalid_code="MANUAL_CONTEXT_FILE_INVALID",
        oversized_code="MANUAL_CONTEXT_CONTENT_INVALID",
        encoding_code="MANUAL_CONTEXT_ENCODING_INVALID",
    )
    context = _normalize_operator_text(decoded, code="MANUAL_CONTEXT_CONTENT_INVALID")
    if len(context) > _MAX_CONTEXT_CHARS:
        raise ManualOpportunityError("MANUAL_CONTEXT_CONTENT_INVALID")
    return context


def s1_relative_thread_ref(target_url: str) -> str:
    """Convert only an already canonical S2 target into S1's relative reference."""

    try:
        normalized = normalize_manual_permalink(target_url)
        parsed = urlsplit(target_url)
    except BrowserContractError, TypeError, ValueError:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH") from None
    if (
        normalized.thread_ref != target_url
        or parsed.scheme != "https"
        or parsed.netloc != "www.threads.com"
    ):
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    parsed_ref = parse_thread_ref(parsed.path)
    if parsed_ref is None or parsed_ref[0] != parsed.path:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    return parsed.path


def build_manual_draft_review(
    account: LocalAccount,
    opportunity: ManualOpportunity,
    draft: str,
    context: str | None,
    *,
    context_completeness: Literal["UNKNOWN", "PARTIAL", "COMPLETE"] = "UNKNOWN",
) -> ManualDraftReview:
    relative_ref = s1_relative_thread_ref(opportunity.target_url)
    identity = json.dumps(
        [str(account.id), opportunity.target_url, draft],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ManualDraftReview(
        account_alias=account.alias,
        account_id=str(account.id),
        opportunity=opportunity,
        relative_thread_ref=relative_ref,
        context=context,
        context_provenance="OPERATOR_SUPPLIED" if context is not None else "NONE",
        context_completeness=context_completeness,
        draft=draft,
        draft_sha256=hashlib.sha256(identity).hexdigest(),
    )


def _render_private_review(
    intake: ManualOpportunityIntake,
    review: ManualDraftReview,
    *,
    queue_persistence: bool = False,
) -> str:
    persistence_note = (
        "This queue item was read or updated locally; no API, browser, or remote action was used."
        if queue_persistence
        else "No API, browser, queue persistence, or remote action was used."
    )
    lines = [
        "MANUAL OPPORTUNITY REVIEW — PRIVATE LOCAL TERMINAL",
        persistence_note,
        f"account alias: {review.account_alias}",
        f"account UUID: {review.account_id}",
        "actor identity: UNKNOWN_IDENTITY",
        "ordered shortlist (one-based selection; no URL-derived score):",
    ]
    selected_index = next(
        index
        for index, candidate in enumerate(intake.candidates, start=1)
        if candidate is review.opportunity
    )
    for index, candidate in enumerate(intake.candidates, start=1):
        status = (
            "OPERATOR_CONTEXT_SUPPLIED_REVIEW_REQUIRED"
            if candidate is review.opportunity and review.context is not None
            else "NEEDS_CONTEXT"
        )
        context_completeness = (
            review.context_completeness if candidate is review.opportunity else "UNKNOWN"
        )
        lines.append(
            f"  {index}. input_position={candidate.input_position} "
            f"source_line={candidate.source_line} status={status} "
            f"provenance=MANUAL_URL target={candidate.target_url} "
            f"permalink_author={candidate.author_username} author_identity=UNVERIFIED "
            f"context_completeness={context_completeness}"
        )
    for duplicate in intake.duplicates:
        lines.append(
            f"  duplicate source_line={duplicate.source_line} "
            f"maps_to_selection={duplicate.shortlist_index} reason=DUPLICATE"
        )
    lines.extend(
        (
            f"selected shortlist item: {selected_index}",
            f"selected target: {review.opportunity.target_url}",
            f"target author from permalink only: {review.opportunity.author_username} "
            + "(identity unverified)",
            f"target provenance: MANUAL_URL input_position={review.opportunity.input_position}",
            f"context provenance: {review.context_provenance}",
            f"context completeness: {review.context_completeness}",
            "operator context: "
            + (
                "<not supplied>"
                if review.context is None
                else json.dumps(review.context, ensure_ascii=False)
            ),
            "operator-authored edited draft: " + json.dumps(review.draft, ensure_ascii=False),
            f"draft sha256: {review.draft_sha256}",
            "status: NOT_SENT",
            "This preview is not permission to post; any later manual action is unverified.",
            "Separate S1 handoff (only after separate live GO; verify actor and target):",
            f"threads-local browser open-thread {review.account_alias} "
            + f'"{review.relative_thread_ref}" --interactive',
        )
    )
    return "\n".join(lines) + "\n"


def render_manual_draft_review(
    intake: ManualOpportunityIntake,
    review: ManualDraftReview,
    *,
    queue_persistence: bool = False,
) -> str:
    """Render the same private S3 review for file and queued opportunities."""

    return _render_private_review(intake, review, queue_persistence=queue_persistence)


def _render_private_shortlist(intake: ManualOpportunityIntake) -> str:
    lines = ["MANUAL OPPORTUNITY SHORTLIST — PRIVATE LOCAL TERMINAL"]
    for index, candidate in enumerate(intake.candidates, start=1):
        lines.append(f"{index}. source_line={candidate.source_line} target={candidate.target_url}")
    for duplicate in intake.duplicates:
        lines.append(
            f"duplicate source_line={duplicate.source_line} "
            f"maps_to_selection={duplicate.shortlist_index} reason=DUPLICATE"
        )
    return "\n".join(lines) + "\n"


def run_manual_list_cli(
    alias: str,
    targets_file: Path,
    *,
    account_store_factory: Callable[[], LocalAccountStore],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Show an offline shortlist on a private TTY without loading any draft."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    if not private_review_terminal_available(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    ):
        raise ManualOpportunityError("PRIVATE_REVIEW_TERMINAL_REQUIRED")

    store = account_store_factory()
    store.get(alias)
    intake = load_manual_opportunities(targets_file)
    private_stderr.write(_render_private_shortlist(intake))
    private_stderr.flush()
    return 0


def run_manual_review_cli(
    alias: str,
    targets_file: Path,
    selection: int,
    draft_file: Path,
    context_file: Path | None,
    *,
    account_store_factory: Callable[[], LocalAccountStore],
    confirm_target: str | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run one ephemeral manual review, writing private material to TTY stderr only."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    if not private_review_terminal_available(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    ):
        raise ManualOpportunityError("PRIVATE_REVIEW_TERMINAL_REQUIRED")

    store = account_store_factory()
    account = store.get(alias)
    intake = load_manual_opportunities(targets_file)
    if type(selection) is not int or not 1 <= selection <= len(intake.candidates):
        raise ManualOpportunityError("MANUAL_SELECTION_INVALID")
    opportunity = intake.candidates[selection - 1]
    if confirm_target is not None and confirm_target != opportunity.target_url:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    draft = load_operator_draft(draft_file)
    context = load_operator_context(context_file)
    review = build_manual_draft_review(
        account,
        opportunity,
        draft,
        context,
    )
    private_stderr.write(render_manual_draft_review(intake, review))
    private_stderr.flush()
    return 0
