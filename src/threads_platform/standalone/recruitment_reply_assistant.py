"""Private, ephemeral reply drafting from the S6 queue and S7 qualification."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TextIO, cast
from uuid import UUID

from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.manual_opportunities import private_review_terminal_available
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityQueueError,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_qualification import (
    OpportunityQualification,
    rank_opportunities,
)
from threads_platform.standalone.recruitment_reply_proposal import (
    DeterministicFakeReplyGenerator,
    EmployerJobFacts,
    ReplyProposalError,
    ReplyProposalGenerator,
    ReplyProposalInput,
    ReplyQualityAdvisory,
    evaluate_reply_quality_text,
    generate_bounded_reply,
    validate_reply_text,
)

_MAX_FACTS_BYTES = 8_192
_MAX_EVIDENCE_EXCERPT_CHARS = 500
_MAX_EDIT_CHARS = 500
_MAX_EDITOR_ACTIONS = 5
_MAX_TIMEOUT_SECONDS = 30.0

AssistantState = Literal["NEEDS_CONTEXT", "SKIPPED", "PROPOSED_FOR_HUMAN_REVIEW"]
EditorialDisposition = Literal["EDITING", "KEPT_FOR_SESSION", "DISCARDED"]


class RecruitmentReplyAssistantError(Exception):
    """Fixed-code errors safe for private terminal output."""

    _CODES = frozenset(
        {
            "PRIVATE_REVIEW_TERMINAL_REQUIRED",
            "OPPORTUNITY_DRAFT_FACTS_INVALID",
            "OPPORTUNITY_DRAFT_FACTS_TOO_LARGE",
            "OPPORTUNITY_DRAFT_INVALID",
            "OPPORTUNITY_DRAFT_GENERATOR_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported reply assistant error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityReplyCandidate:
    account_uuid: str
    opportunity_id: str
    target_url: str = field(repr=False)
    context: str = field(repr=False)
    evidence_excerpt: str = field(repr=False)
    evidence_excerpt_truncated: bool
    context_provenance: Literal["OPERATOR_SUPPLIED"]
    context_completeness: Literal["COMPLETE"]
    employer_facts: EmployerJobFacts | None = field(repr=False)
    qualification: OpportunityQualification = field(repr=False)

    def __post_init__(self) -> None:
        try:
            account_id = UUID(self.account_uuid)
            opportunity_id = UUID(self.opportunity_id)
        except TypeError, ValueError, AttributeError:
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID") from None
        if (
            account_id.version != 4
            or str(account_id) != self.account_uuid
            or opportunity_id.version != 4
            or str(opportunity_id) != self.opportunity_id
            or type(self.target_url) is not str
            or not self.target_url
            or type(self.context) is not str
            or not self.context.strip()
            or len(self.context) > 4_000
            or type(self.evidence_excerpt) is not str
            or self.evidence_excerpt != self.context[:_MAX_EVIDENCE_EXCERPT_CHARS].strip()
            or type(self.evidence_excerpt_truncated) is not bool
            or self.evidence_excerpt_truncated != (len(self.context) > _MAX_EVIDENCE_EXCERPT_CHARS)
            or self.context_provenance != "OPERATOR_SUPPLIED"
            or self.context_completeness != "COMPLETE"
            or (
                self.employer_facts is not None
                and type(self.employer_facts) is not EmployerJobFacts
            )
            or type(self.qualification) is not OpportunityQualification
            or self.qualification.account_uuid != self.account_uuid
            or self.qualification.opportunity_id != self.opportunity_id
            or self.qualification.target_url != self.target_url
            or self.qualification.classification != "JOB_SEEKER"
            or self.qualification.state != "ELIGIBLE_FOR_REVIEW"
        ):
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")

    def __repr__(self) -> str:
        return (
            "OpportunityReplyCandidate("
            f"account_uuid={self.account_uuid!r}, opportunity_id={self.opportunity_id!r}, "
            "target_url=<redacted>, context=<redacted>, employer_facts=<redacted>, "
            "qualification=<bound>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityReplyProposal:
    candidate: OpportunityReplyCandidate = field(repr=False)
    suggested_reply: str = field(repr=False)
    source: Literal["FAKE_SYNTHETIC"] = "FAKE_SYNTHETIC"
    status: Literal["PROPOSED_FOR_HUMAN_REVIEW"] = "PROPOSED_FOR_HUMAN_REVIEW"
    approval_status: Literal["NOT_APPROVED_FOR_SENDING"] = "NOT_APPROVED_FOR_SENDING"
    delivery_status: Literal["NOT_SENT"] = "NOT_SENT"

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not OpportunityReplyCandidate
            or type(self.source) is not str
            or self.source != "FAKE_SYNTHETIC"
            or type(self.status) is not str
            or self.status != "PROPOSED_FOR_HUMAN_REVIEW"
            or self.approval_status != "NOT_APPROVED_FOR_SENDING"
            or self.delivery_status != "NOT_SENT"
        ):
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
        validate_reply_text(self.suggested_reply)

    def __repr__(self) -> str:
        return (
            "OpportunityReplyProposal(source=FAKE_SYNTHETIC, "
            "status=PROPOSED_FOR_HUMAN_REVIEW, approval_status=NOT_APPROVED_FOR_SENDING, "
            "delivery_status=NOT_SENT, suggested_reply=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityReplyPreview:
    candidate: OpportunityReplyCandidate = field(repr=False)
    proposal: OpportunityReplyProposal = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not OpportunityReplyCandidate
            or type(self.proposal) is not OpportunityReplyProposal
            or self.proposal.candidate is not self.candidate
        ):
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")

    def __repr__(self) -> str:
        return "OpportunityReplyPreview(candidate=<bound>, proposal=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityReplyEditorialSession:
    """In-memory editing session; keeping a draft is not approval to send."""

    preview: OpportunityReplyPreview = field(repr=False)
    current_reply: str | None = field(repr=False)
    disposition: EditorialDisposition = "EDITING"

    def __post_init__(self) -> None:
        if (
            type(self.preview) is not OpportunityReplyPreview
            or type(self.disposition) is not str
            or self.disposition not in {"EDITING", "KEPT_FOR_SESSION", "DISCARDED"}
        ):
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
        if self.disposition == "DISCARDED":
            if self.current_reply is not None:
                raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
        else:
            validate_reply_text(self.current_reply)

    @classmethod
    def begin(cls, preview: OpportunityReplyPreview) -> OpportunityReplyEditorialSession:
        if type(preview) is not OpportunityReplyPreview:
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
        return cls(preview, preview.proposal.suggested_reply)

    @property
    def candidate(self) -> OpportunityReplyCandidate:
        return self.preview.candidate

    @property
    def approval_status(self) -> Literal["NOT_APPROVED_FOR_SENDING"]:
        return "NOT_APPROVED_FOR_SENDING"

    @property
    def human_review_status(self) -> Literal["PENDING", "APPROVED_FOR_SESSION", "REJECTED"]:
        if self.disposition == "KEPT_FOR_SESSION":
            return "APPROVED_FOR_SESSION"
        if self.disposition == "DISCARDED":
            return "REJECTED"
        return "PENDING"

    @property
    def delivery_status(self) -> Literal["NOT_SENT"]:
        return "NOT_SENT"

    @property
    def draft_persisted(self) -> Literal[False]:
        return False

    @property
    def quality_advisory(self) -> ReplyQualityAdvisory | None:
        if self.current_reply is None:
            return None
        return evaluate_reply_quality_text(self.current_reply)

    def edit(self, value: object) -> OpportunityReplyEditorialSession:
        self._require_editing()
        validate_reply_text(value)
        return OpportunityReplyEditorialSession(self.preview, cast(str, value), "EDITING")

    def keep_for_session(self) -> OpportunityReplyEditorialSession:
        self._require_editing()
        return OpportunityReplyEditorialSession(
            self.preview, self.current_reply, "KEPT_FOR_SESSION"
        )

    def discard(self) -> OpportunityReplyEditorialSession:
        self._require_editing()
        return OpportunityReplyEditorialSession(self.preview, None, "DISCARDED")

    def _require_editing(self) -> None:
        if self.disposition != "EDITING":
            raise ReplyProposalError("EDITOR_SESSION_CLOSED")

    def __repr__(self) -> str:
        return (
            "OpportunityReplyEditorialSession("
            f"disposition={self.disposition}, current_reply=<redacted>, "
            "approval_status=NOT_APPROVED_FOR_SENDING, delivery_status=NOT_SENT)"
        )


async def build_opportunity_reply_preview(
    entry: OpportunityQueueEntry,
    qualification: OpportunityQualification,
    generator: ReplyProposalGenerator,
    *,
    account_uuid: str,
    facts: EmployerJobFacts | None = None,
    timeout_seconds: float = 5.0,
) -> OpportunityReplyPreview:
    """Create one fake proposal bound to the exact account, queue item, and evidence."""

    try:
        account_id = UUID(account_uuid)
    except TypeError, ValueError, AttributeError:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID") from None
    if (
        account_id.version != 4
        or str(account_id) != account_uuid
        or type(entry) is not OpportunityQueueEntry
        or type(qualification) is not OpportunityQualification
        or entry.id != qualification.opportunity_id
        or entry.target_url != qualification.target_url
        or entry.source != "MANUAL_URL"
        or entry.state != "READY"
        or entry.context is None
        or entry.context_provenance != "OPERATOR_SUPPLIED"
        or entry.context_completeness != "COMPLETE"
        or qualification.state != "ELIGIBLE_FOR_REVIEW"
        or qualification.classification != "JOB_SEEKER"
        or qualification.account_uuid != account_uuid
        or (facts is not None and type(facts) is not EmployerJobFacts)
        or type(timeout_seconds) not in {int, float}
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")

    excerpt = entry.context[:_MAX_EVIDENCE_EXCERPT_CHARS].strip()
    if not excerpt:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
    reason_codes = qualification.reason_codes[:24]
    language = _detect_language(entry.context)
    request = ReplyProposalInput(
        source="MANUAL_URL",
        score=qualification.score,
        reason_codes=reason_codes,
        candidate_text=excerpt,
        account_uuid=account_uuid,
        target_id=entry.id,
        context_provenance="OPERATOR_SUPPLIED",
        context_completeness="COMPLETE",
        recipient_intent="JOB_SEEKER",
        language=language,
        job_family=qualification.job_family,
        location_signal=qualification.location_signal,
        student_signal="STUDENT_SIGNAL" in reason_codes,
        part_time_signal="PART_TIME_SIGNAL" in reason_codes,
        internship_signal="INTERNSHIP_SIGNAL" in reason_codes,
        employer_facts=facts,
    )
    generated = await generate_bounded_reply(
        request,
        generator,
        timeout_seconds=float(timeout_seconds),
    )
    candidate = OpportunityReplyCandidate(
        account_uuid=account_uuid,
        opportunity_id=entry.id,
        target_url=entry.target_url,
        context=entry.context,
        evidence_excerpt=excerpt,
        evidence_excerpt_truncated=len(entry.context) > _MAX_EVIDENCE_EXCERPT_CHARS,
        context_provenance="OPERATOR_SUPPLIED",
        context_completeness="COMPLETE",
        employer_facts=facts,
        qualification=qualification,
    )
    proposal = OpportunityReplyProposal(candidate, generated)
    return OpportunityReplyPreview(candidate, proposal)


def run_opportunity_draft_cli(
    alias: str,
    opportunity_id: str,
    *,
    facts_file: Path | None = None,
    data_root_factory: Callable[[], Path],
    generator_factory: Callable[[], ReplyProposalGenerator] = DeterministicFakeReplyGenerator,
    timeout_seconds: float = 5.0,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Review one S6 queue item in a private TTY without persistence or sending."""

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
        raise RecruitmentReplyAssistantError("PRIVATE_REVIEW_TERMINAL_REQUIRED")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_GENERATOR_INVALID")

    data_root = Path(data_root_factory())
    account = LocalAccountStore(data_root).get(alias)
    queue = OpportunityQueueStore(data_root).list(account.id)
    selected = next(
        (entry for entry in queue.opportunities if entry.id == opportunity_id),
        None,
    )
    if selected is None:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")
    if selected.state == "SKIPPED":
        _write_no_draft(
            private_stderr,
            alias=account.alias,
            account_uuid=str(account.id),
            entry=selected,
            state="SKIPPED",
            reason_codes=("QUEUE_ENTRY_SKIPPED",),
        )
        return 0

    active_entries = tuple(entry for entry in queue.opportunities if entry.state != "SKIPPED")
    active_queue = OpportunityQueue(queue.account_uuid, active_entries)
    outcome_store = ManualOutcomeStore(data_root)
    s4_outcomes: dict[str, str] = {}
    for entry in active_entries:
        outcome = outcome_store.read(account.id, entry.target_url)
        if outcome is not None:
            s4_outcomes[entry.target_url] = outcome.latest.status
    ranking = rank_opportunities(active_queue, limit=20, s4_outcomes=s4_outcomes)
    qualification = next(
        (item for item in ranking.items if item.opportunity_id == selected.id),
        None,
    )
    if qualification is None:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_INVALID")
    if qualification.state != "ELIGIBLE_FOR_REVIEW":
        state: AssistantState = (
            "NEEDS_CONTEXT" if qualification.state == "NEEDS_CONTEXT" else "SKIPPED"
        )
        _write_no_draft(
            private_stderr,
            alias=account.alias,
            account_uuid=str(account.id),
            entry=selected,
            state=state,
            reason_codes=qualification.reason_codes,
        )
        return 0

    facts = None if facts_file is None else _load_job_facts(Path(facts_file))
    try:
        generator = generator_factory()
    except Exception:  # noqa: BLE001 — generator setup may contain private details.
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_GENERATOR_INVALID") from None
    preview = asyncio.run(
        build_opportunity_reply_preview(
            selected,
            qualification,
            generator,
            account_uuid=str(account.id),
            facts=facts,
            timeout_seconds=float(timeout_seconds),
        )
    )
    session = OpportunityReplyEditorialSession.begin(preview)
    _render_editorial_session(
        private_stderr,
        alias=account.alias,
        account_uuid=str(account.id),
        session=session,
    )
    session = _edit_loop(session, private_stdin, private_stderr, account.alias)
    _render_editorial_session(
        private_stderr,
        alias=account.alias,
        account_uuid=str(account.id),
        session=session,
    )
    private_stderr.flush()
    return 0


def _write_no_draft(
    stream: TextIO,
    *,
    alias: str,
    account_uuid: str,
    entry: OpportunityQueueEntry,
    state: Literal["NEEDS_CONTEXT", "SKIPPED"],
    reason_codes: tuple[str, ...],
) -> None:
    context_display = (
        "<not supplied>" if entry.context is None else json.dumps(entry.context, ensure_ascii=False)
    )
    stream.write(
        "RECRUITMENT REPLY ASSISTANT — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {alias}\n"
        f"account UUID: {account_uuid}\n"
        f"opportunity id: {entry.id}\n"
        f"state: {state}\n"
        f"source: {entry.source}\n"
        f"exact target: {entry.target_url}\n"
        f"context provenance: {entry.context_provenance}\n"
        f"context completeness: {entry.context_completeness}\n"
        f"source context: {context_display}\n"
        f"reasons: {','.join(reason_codes)}\n"
        "draft: <not generated>\n"
        "approval status: NOT_APPROVED_FOR_SENDING\n"
        "delivery status: NOT_SENT\n"
        "draft persisted: NO\n"
    )
    stream.flush()


def _render_editorial_session(
    stream: TextIO,
    *,
    alias: str,
    account_uuid: str,
    session: OpportunityReplyEditorialSession,
) -> None:
    candidate = session.candidate
    qualification = candidate.qualification
    context_display = json.dumps(candidate.context, ensure_ascii=False)
    current_reply = "<discarded>" if session.current_reply is None else session.current_reply
    quality = session.quality_advisory
    quality_flags = "NONE" if quality is None or not quality.flags else ",".join(quality.flags)
    facts = candidate.employer_facts
    if facts is None:
        facts_display = "<not supplied>"
    else:
        fact_values = {
            "role": facts.role,
            "location": facts.location,
            "pay_range": facts.pay_range,
            "eligibility": facts.eligibility,
            "verified_contact_available": facts.verified_contact_available,
            "provenance": facts.provenance,
        }
        facts_display = json.dumps(fact_values, ensure_ascii=False, sort_keys=True)
    stream.write(
        "RECRUITMENT REPLY ASSISTANT — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {alias}\n"
        f"account UUID: {account_uuid}\n"
        f"opportunity id: {candidate.opportunity_id}\n"
        f"qualification: {qualification.classification} {qualification.state}\n"
        f"qualification reasons: {','.join(qualification.reason_codes)}\n"
        f"source: MANUAL_URL\n"
        f"exact target: {candidate.target_url}\n"
        f"context provenance: {candidate.context_provenance}\n"
        f"context completeness: {candidate.context_completeness}\n"
        f"source context: {context_display}\n"
        f"evidence excerpt (truncated={str(candidate.evidence_excerpt_truncated).lower()}): "
        f"{json.dumps(candidate.evidence_excerpt, ensure_ascii=False)}\n"
        f"employer facts: {facts_display}\n"
        "draft source: FAKE_SYNTHETIC (offline deterministic template)\n"
        f"draft status: {session.preview.proposal.status}\n"
        f"editorial disposition: {session.disposition}\n"
        f"human review: {session.human_review_status}\n"
        f"quality advisory: {quality_flags}\n"
        f"exact draft: {current_reply}\n"
        f"approval status: {session.approval_status}\n"
        f"delivery status: {session.delivery_status}\n"
        "draft persisted: NO\n"
    )
    if session.disposition == "EDITING":
        stream.write("action: E edit, K approve for this session, D discard\n")


def _edit_loop(
    session: OpportunityReplyEditorialSession,
    stdin: TextIO,
    stderr: TextIO,
    alias: str,
) -> OpportunityReplyEditorialSession:
    for _ in range(_MAX_EDITOR_ACTIONS):
        action = _read_bounded_line(stdin, 4).upper()
        if action == "K":
            return session.keep_for_session()
        if action == "D" or action == "":
            return session.discard()
        if action == "E":
            stderr.write("edited draft (one line, max 500 characters):\n")
            value = _read_bounded_line(stdin, _MAX_EDIT_CHARS)
            try:
                session = session.edit(value)
            except ReplyProposalError:
                stderr.write("edit rejected; draft discarded\n")
                return session.discard()
            _render_editorial_session(
                stderr,
                alias=alias,
                account_uuid=session.candidate.account_uuid,
                session=session,
            )
            continue
        stderr.write("action rejected; draft discarded\n")
        return session.discard()
    stderr.write("edit limit reached; draft discarded\n")
    return session.discard()


def _read_bounded_line(stream: TextIO, limit: int) -> str:
    try:
        value = stream.readline(limit + 3)
    except KeyboardInterrupt:
        return ""
    if not value:
        return ""
    line = value.rstrip("\r\n")
    if len(line) > limit or "\n" in line or "\r" in line:
        return "\0"
    return line


def _load_job_facts(path: Path) -> EmployerJobFacts:
    try:
        info = path.lstat()
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse_attribute)
        ):
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
        if info.st_size > _MAX_FACTS_BYTES:
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_TOO_LARGE")
        raw = path.read_bytes()
    except RecruitmentReplyAssistantError:
        raise
    except OSError, TypeError, ValueError:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID") from None
    if len(raw) > _MAX_FACTS_BYTES:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_TOO_LARGE")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeError, ValueError, json.JSONDecodeError:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID") from None
    if type(document) is not dict:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
    values = cast(dict[str, object], document)
    if set(values) != {
        "provenance",
        "role",
        "location",
        "pay_range",
        "eligibility",
        "verified_contact_available",
    }:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
    if values["provenance"] != "OPERATOR_ASSERTED_VERIFIED":
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
    optional_strings: dict[str, str | None] = {}
    for key in ("role", "location", "pay_range", "eligibility"):
        value = values[key]
        if value is not None and type(value) is not str:
            raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
        optional_strings[key] = value
    contact_available = values["verified_contact_available"]
    if type(contact_available) is not bool:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID")
    try:
        return EmployerJobFacts(
            role=optional_strings["role"],
            location=optional_strings["location"],
            pay_range=optional_strings["pay_range"],
            eligibility=optional_strings["eligibility"],
            verified_contact_available=contact_available,
            provenance="OPERATOR_ASSERTED_VERIFIED",
        )
    except ReplyProposalError:
        raise RecruitmentReplyAssistantError("OPPORTUNITY_DRAFT_FACTS_INVALID") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant")


def _detect_language(context: str) -> Literal["vi", "en"]:
    lowered = context.casefold()
    if any(
        phrase in lowered
        for phrase in ("đang tìm", "tôi", "mình", "việc làm", "thực tập", "xin việc")
    ) or any(
        mark in unicodedata.name(character, "")
        for character in context
        for mark in ("WITH DOT BELOW", "WITH HOOK ABOVE", "WITH HORN", "WITH BREVE", "WITH STROKE")
    ):
        return "vi"
    return "en"
