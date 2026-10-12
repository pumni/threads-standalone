from __future__ import annotations

import builtins
import io
import os
import socket
import subprocess
from dataclasses import replace
from typing import cast

import pytest

from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeRecord,
)
from threads_platform.standalone.opportunity_queue import (
    ContextCompleteness,
    ContextProvenance,
    OpportunityQueue,
    OpportunityQueueEntry,
)
from threads_platform.standalone.recruitment_crm_export import (
    DEFAULT_EXPORT_FIELDS,
    RecruitmentExportApproval,
    RecruitmentExportField,
    RecruitmentExportPurpose,
    create_recruitment_export_approval,
)
from threads_platform.standalone.recruitment_crm_review import (
    CrmExportReviewPacketV1,
    build_crm_export_review_packet,
    required_crm_export_confirmation,
)
from threads_platform.standalone.recruitment_crm_review_tty import (
    CrmReviewTtyError,
    _safe_value,
    run_crm_review_on_private_tty,
)

ACCOUNT_A = "123e4567-e89b-42d3-a456-426614174000"
ACCOUNT_B = "123e4567-e89b-42d3-a456-426614174001"
REVIEWED_AT = "2026-10-12T08:00:00Z"
EXPIRES_AT = "2026-10-13T08:00:00Z"
NOW = "2026-10-12T08:30:00Z"
DESTINATION_A = f"sha256:{'a' * 64}"
PRIVATE_CONTEXT = "SYNTHETIC_PRIVATE_CONTEXT: private@example.invalid"


class FakeTty(io.StringIO):
    def __init__(self, initial: str = "", *, tty: object = True) -> None:
        super().__init__(initial)
        self._tty = tty

    def isatty(self) -> bool:
        return cast(bool, self._tty)


class InterruptingTty(FakeTty):
    def readline(self, size: int = -1) -> str:
        raise KeyboardInterrupt


class MutatingTty(FakeTty):
    def __init__(self, records: dict[str, ManualOutcomeRecord | None], line: str) -> None:
        super().__init__(line)
        self._records = records

    def readline(self, size: int = -1) -> str:
        self._records.clear()
        return super().readline(size)


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012x}"


def _entry(index: int, *, account_seed: int = 0) -> OpportunityQueueEntry:
    return OpportunityQueueEntry(
        id=_uuid(1_270_000 + account_seed * 100 + index),
        target_url=f"https://www.threads.com/@synthetic/post/S12B2B1{index:04d}",
        source="MANUAL_URL",
        context=PRIVATE_CONTEXT,
        context_provenance=cast(ContextProvenance, "OPERATOR_SUPPLIED"),
        context_completeness=cast(ContextCompleteness, "COMPLETE"),
        created_at=REVIEWED_AT,
        updated_at=REVIEWED_AT,
        state="READY",
        skip_reason=None,
    )


def _queue(*entries: OpportunityQueueEntry, account_uuid: str = ACCOUNT_A) -> OpportunityQueue:
    return OpportunityQueue(account_uuid=account_uuid, opportunities=tuple(entries))


def _records(queue: OpportunityQueue) -> dict[str, ManualOutcomeRecord | None]:
    return {entry.target_url: None for entry in queue.opportunities}


def _approval(
    queue: OpportunityQueue,
    entry: OpportunityQueueEntry,
    *,
    fields: tuple[RecruitmentExportField, ...] = DEFAULT_EXPORT_FIELDS,
) -> RecruitmentExportApproval:
    return create_recruitment_export_approval(
        queue,
        entry.id,
        purpose=RecruitmentExportPurpose.FOLLOW_UP,
        lawful_basis_asserted=True,
        rights_asserted=True,
        consent_asserted=True,
        reviewed_at=REVIEWED_AT,
        expires_at=EXPIRES_AT,
        selected_fields=fields,
    )


def _packet(
    queue: OpportunityQueue,
    selected_ids: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    records: dict[str, ManualOutcomeRecord | None] | None = None,
    *,
    destination_identifier: str = DESTINATION_A,
) -> CrmExportReviewPacketV1:
    return build_crm_export_review_packet(
        queue,
        _records(queue) if records is None else records,
        selected_ids,
        approvals,
        destination_identifier=destination_identifier,
        now_utc=NOW,
    )


def _invoke(
    packet: CrmExportReviewPacketV1,
    queue: OpportunityQueue,
    records: dict[str, ManualOutcomeRecord | None],
    approvals: tuple[RecruitmentExportApproval, ...],
    selected_ids: tuple[str, ...],
    *,
    destination_identifier: str = DESTINATION_A,
    stdin: io.StringIO | None = None,
    stdout: io.StringIO | None = None,
    stderr: io.StringIO | None = None,
    environment: dict[str, str] | None = None,
    now_utc: str = NOW,
) -> tuple[str, io.StringIO, io.StringIO, io.StringIO]:
    input_stream = (
        FakeTty(required_crm_export_confirmation(packet) + "\n") if stdin is None else stdin
    )
    output_stream = FakeTty() if stdout is None else stdout
    error_stream = FakeTty() if stderr is None else stderr
    result = run_crm_review_on_private_tty(
        packet,
        queue,
        records,
        approvals,
        selected_ids,
        destination_identifier,
        stdin=input_stream,
        stdout=output_stream,
        stderr=error_stream,
        environment={} if environment is None else environment,
        now_utc=now_utc,
    )
    return result, output_stream, error_stream, input_stream


def test_private_review_renders_only_selected_exact_fields_and_returns_match_only() -> None:
    selected = _entry(1)
    selected_url_opt_in = _entry(2)
    unselected = _entry(3)
    queue = _queue(selected, selected_url_opt_in, unselected)
    selected_approval = _approval(queue, selected)
    url_approval = _approval(
        queue,
        selected_url_opt_in,
        fields=(*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL),
    )
    approvals = (selected_approval, url_approval)
    selected_ids = (selected.id, selected_url_opt_in.id)
    packet = _packet(queue, selected_ids, approvals)

    result, stdout, stderr, _ = _invoke(packet, queue, _records(queue), approvals, selected_ids)
    review = stdout.getvalue()

    assert result == "SNAPSHOT_MATCH_ONLY"
    assert selected.target_url in review
    assert selected_url_opt_in.target_url in review
    assert unselected.target_url not in review
    assert selected.id in review and selected_url_opt_in.id in review
    assert review.index(selected.id) < review.index(selected_url_opt_in.id)
    assert "TARGET_URL export choice: NOT_OPTED_IN" in review
    assert review.count("TARGET_URL export choice: OPTED_IN") == 1
    assert "TARGET_URL" in review
    assert "RECRUITMENT_FOLLOW_UP" in review
    assert "ASSERTED_NOT_INDEPENDENTLY_VERIFIED" in review
    assert DESTINATION_A in review
    assert PRIVATE_CONTEXT not in review
    assert "private@example.invalid" not in review
    assert '{"schema"' not in review
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    "environment",
    [{"CI": "true"}, {"GITHUB_ACTIONS": "false"}, {"JENKINS_URL": "https://ci.invalid"}],
)
def test_ci_environment_fails_before_any_private_value_is_printed(
    environment: dict[str, str],
) -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdout=stdout,
            environment=environment,
        )

    assert caught.value.code == "CRM_REVIEW_TTY_CI_FORBIDDEN"
    assert stdout.getvalue() == ""
    assert entry.target_url not in str(caught.value)


@pytest.mark.parametrize("stream_name", ["stdin", "stdout", "stderr"])
def test_non_tty_or_redirected_stream_fails_before_review_display(stream_name: str) -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    streams: dict[str, io.StringIO] = {
        "stdin": FakeTty(required_crm_export_confirmation(packet) + "\n", tty=False),
        "stdout": FakeTty(tty=False),
        "stderr": FakeTty(tty=False),
    }
    stdout = streams["stdout"] if stream_name == "stdout" else FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdin=streams["stdin"]
            if stream_name == "stdin"
            else FakeTty(required_crm_export_confirmation(packet) + "\n"),
            stdout=stdout,
            stderr=streams["stderr"] if stream_name == "stderr" else FakeTty(),
        )

    assert caught.value.code == "CRM_REVIEW_TTY_REQUIRED"
    assert stdout.getvalue() == ""


def test_isatty_must_return_the_boolean_true_singleton() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdout=FakeTty(tty=1),
        )

    assert caught.value.code == "CRM_REVIEW_TTY_REQUIRED"


def test_changed_account_or_target_fails_before_display() -> None:
    entry_a = _entry(1)
    queue_a = _queue(entry_a)
    approval_a = _approval(queue_a, entry_a)
    packet_a = _packet(queue_a, (entry_a.id,), (approval_a,))

    entry_b = _entry(1, account_seed=1)
    queue_b = _queue(entry_b, account_uuid=ACCOUNT_B)
    approval_b = _approval(queue_b, entry_b)
    stdout = FakeTty()
    with pytest.raises(CrmReviewTtyError) as account_error:
        _invoke(packet_a, queue_b, _records(queue_b), (approval_b,), (entry_b.id,), stdout=stdout)
    assert account_error.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""

    changed_target = replace(entry_a, target_url="https://www.threads.com/@synthetic/post/CHANGED")
    changed_queue = _queue(changed_target)
    stdout = FakeTty()
    with pytest.raises(CrmReviewTtyError) as target_error:
        _invoke(
            packet_a,
            changed_queue,
            _records(changed_queue),
            (approval_a,),
            (entry_a.id,),
            stdout=stdout,
        )
    assert target_error.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""


def test_changed_destination_or_item_field_choice_fails_before_display() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as destination_error:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            destination_identifier=f"sha256:{'b' * 64}",
            stdout=stdout,
        )
    assert destination_error.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""

    changed_approval = _approval(
        queue,
        entry,
        fields=(*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL),
    )
    stdout = FakeTty()
    with pytest.raises(CrmReviewTtyError) as field_error:
        _invoke(
            packet,
            queue,
            _records(queue),
            (changed_approval,),
            (entry.id,),
            stdout=stdout,
        )
    assert field_error.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""


def test_stale_s4_snapshot_is_rejected_before_display() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(packet, queue, {}, (approval,), (entry.id,), stdout=stdout)

    assert caught.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""


def test_expired_approval_is_rejected_before_display() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdout=stdout,
            now_utc="2026-10-13T08:00:00Z",
        )

    assert caught.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""


def test_state_change_while_waiting_for_input_fails_after_display() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    records = _records(queue)
    packet = _packet(queue, (entry.id,), (approval,), records)
    stdin = MutatingTty(records, required_crm_export_confirmation(packet) + "\n")
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(packet, queue, records, (approval,), (entry.id,), stdin=stdin, stdout=stdout)

    assert caught.value.code == "CRM_REVIEW_TTY_STALE"
    assert entry.target_url in stdout.getvalue()
    assert entry.target_url not in str(caught.value)


@pytest.mark.parametrize(
    ("stdin", "expected_code"),
    [
        (FakeTty(), "CRM_REVIEW_TTY_CONFIRMATION_REQUIRED"),
        (FakeTty("NO\n"), "CRM_REVIEW_TTY_CONFIRMATION_MISMATCH"),
        (FakeTty("EXPORT\n"), "CRM_REVIEW_TTY_CONFIRMATION_MISMATCH"),
        (InterruptingTty(), "CRM_REVIEW_TTY_INTERRUPTED"),
    ],
)
def test_eof_wrong_confirmation_and_interrupt_fail_closed(
    stdin: FakeTty,
    expected_code: str,
) -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(packet, queue, _records(queue), (approval,), (entry.id,), stdin=stdin)

    assert caught.value.code == expected_code
    assert entry.target_url not in str(caught.value)


def test_long_or_unterminated_confirmation_fails_closed() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    for response in ("x" * 500 + "\n", required_crm_export_confirmation(packet)):
        with pytest.raises(CrmReviewTtyError) as caught:
            _invoke(
                packet, queue, _records(queue), (approval,), (entry.id,), stdin=FakeTty(response)
            )
        assert caught.value.code == "CRM_REVIEW_TTY_CONFIRMATION_MISMATCH"


def test_stream_errors_are_fixed_and_do_not_echo_private_data() -> None:
    class BrokenWriter(FakeTty):
        def write(self, value: str) -> int:
            raise OSError("raw private path and target details")

    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdout=BrokenWriter(),
        )
    assert caught.value.code == "CRM_REVIEW_TTY_IO_ERROR"
    assert entry.target_url not in repr(caught.value)
    assert "raw private path" not in str(caught.value)

    class BrokenReader(FakeTty):
        def readline(self, size: int = -1) -> str:
            raise OSError("synthetic private stream failure")

    with pytest.raises(CrmReviewTtyError) as input_error:
        _invoke(
            packet,
            queue,
            _records(queue),
            (approval,),
            (entry.id,),
            stdin=BrokenReader(),
        )
    assert input_error.value.code == "CRM_REVIEW_TTY_IO_ERROR"
    assert entry.target_url not in str(input_error.value)
    assert "synthetic private stream failure" not in repr(input_error.value)


def test_untrusted_control_character_target_is_rejected_without_rendering() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    unsafe_entry = replace(
        entry,
        target_url="https://www.threads.com/@synthetic/post/SAFE\x1b[31m",
    )
    unsafe_queue = _queue(unsafe_entry)
    stdout = FakeTty()

    with pytest.raises(CrmReviewTtyError) as caught:
        _invoke(
            packet,
            unsafe_queue,
            _records(unsafe_queue),
            (approval,),
            (entry.id,),
            stdout=stdout,
        )

    assert caught.value.code == "CRM_REVIEW_TTY_STALE"
    assert stdout.getvalue() == ""
    assert "\x1b" not in str(caught.value)


def test_render_escape_and_unicode_validation_are_fail_closed() -> None:
    assert _safe_value("line\n\x1b[31m", 32) == r"line\n\u001b[31m"
    for invalid in ("e\u0301", "surrogate-\ud800"):
        with pytest.raises(CrmReviewTtyError) as caught:
            _safe_value(invalid, 32)
        assert caught.value.code == "CRM_REVIEW_TTY_UNSAFE_VALUE"


def test_review_has_no_file_network_subprocess_or_external_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected external side effect")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    result, stdout, stderr, _ = _invoke(packet, queue, _records(queue), (approval,), (entry.id,))

    assert result == "SNAPSHOT_MATCH_ONLY"
    assert stdout.getvalue()
    assert stderr.getvalue() == ""
