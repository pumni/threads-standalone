from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.request
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from threads_platform.standalone.recruitment_content_planner import (
    ContentAttachmentMetadataV1,
    RecruitmentContentCreateResult,
    RecruitmentContentPlannerError,
    RecruitmentContentPlannerStore,
)

_ACCOUNT_ONE = UUID("11111111-1111-4111-8111-111111111111")
_ACCOUNT_TWO = UUID("22222222-2222-4222-8222-222222222222")
_NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
_NAMESPACE = "recruitment_content_planner_v1"
_TEXT = (
    "Strong recruitment starts with a clear role brief, a predictable interview timeline, "
    "and a useful update after each conversation."
)


class _Clock:
    def __init__(self, value: datetime = _NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _store(root: Path, clock: _Clock | None = None) -> RecruitmentContentPlannerStore:
    return RecruitmentContentPlannerStore(root, utc_now=clock or _Clock())


def _create(
    store: RecruitmentContentPlannerStore,
    account_id: UUID = _ACCOUNT_ONE,
    text: str = _TEXT,
    *,
    scheduled_at: datetime = _NOW + timedelta(days=1),
    private_notes: str | None = None,
    attachment: ContentAttachmentMetadataV1 | None = None,
) -> RecruitmentContentCreateResult:
    return store.create(
        account_id,
        text,
        scheduled_at,
        provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
        attachment=attachment,
        private_notes=private_notes,
    )


def _queue_path(root: Path, account_id: UUID = _ACCOUNT_ONE) -> Path:
    return root / _NAMESPACE / "accounts" / f"{account_id}.json"


def _unique_text(index: int) -> str:
    return (
        f"Recruitment planning idea {index}: share a concise role overview, explain "
        "the interview stages, and set a clear update expectation."
    )


def _write_private_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _replace_packet_fields(
    root: Path,
    changes: dict[str, object],
    account_id: UUID = _ACCOUNT_ONE,
) -> bytes:
    path = _queue_path(root, account_id)
    document = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    packets = cast(list[object], document["packets"])
    packet = cast(dict[str, object], packets[0])
    packets[0] = {**packet, **changes}
    _write_private_json(path, document)
    return path.read_bytes()


def test_original_packet_is_versioned_private_and_never_marks_remote_publication(
    tmp_path: Path,
) -> None:
    result = _create(
        _store(tmp_path),
        private_notes="Drafted during the October editorial review.",
        attachment=ContentAttachmentMetadataV1(
            kind="IMAGE",
            url="https://example.com/recruitment-graphic.png",
            label="Recruitment graphic",
            attribution="Created by our team",
            rights_asserted=True,
            consent_asserted=True,
        ),
    )
    packet = result.packet
    persisted = json.loads(_queue_path(tmp_path).read_text(encoding="utf-8"))

    assert packet.version == 1
    assert packet.account_uuid == _ACCOUNT_ONE
    assert packet.packet_id.version == 4
    assert packet.original_text == _TEXT
    assert packet.content_provenance == "OPERATOR_AUTHORED"
    assert packet.rights_asserted is True
    assert packet.consent_asserted is True
    assert packet.rights_assertion_status == "UNVERIFIED_OPERATOR_ASSERTION"
    assert packet.platform_permission_status == "NOT_ESTABLISHED"
    assert packet.editorial_status == "DRAFT"
    assert packet.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert packet.attachment is not None
    assert packet.attachment.url == "https://example.com/recruitment-graphic.png"
    assert persisted["version"] == 1
    assert persisted["account_uuid"] == str(_ACCOUNT_ONE)
    assert persisted["packets"][0]["account_uuid"] == str(_ACCOUNT_ONE)
    assert "PUBLISHED" not in persisted["packets"][0]["editorial_status"]

    approved = _store(tmp_path).approve(_ACCOUNT_ONE, packet.packet_id)
    assert approved.editorial_status == "READY_FOR_MANUAL_PUBLICATION"
    assert approved.reviewed_at == _NOW
    assert approved.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert not hasattr(_store(tmp_path), "publish")


def test_two_account_calendars_are_isolated_and_ordered_by_utc_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _create(store, _ACCOUNT_ONE, _TEXT, scheduled_at=_NOW + timedelta(days=2)).packet
    second_text = "Recruiting teams can make hiring clearer with a simple interview plan."
    second = _create(
        store, _ACCOUNT_ONE, second_text, scheduled_at=_NOW + timedelta(hours=2)
    ).packet
    other = _create(store, _ACCOUNT_TWO, _TEXT, scheduled_at=_NOW + timedelta(hours=1)).packet

    first_calendar = store.list(_ACCOUNT_ONE)
    second_calendar = store.list(_ACCOUNT_TWO)
    assert first_calendar.account_uuid == _ACCOUNT_ONE
    assert tuple(packet.packet_id for packet in first_calendar.packets) == (
        second.packet_id,
        first.packet_id,
    )
    assert tuple(packet.packet_id for packet in second_calendar.packets) == (other.packet_id,)
    assert first.packet_id != other.packet_id
    assert _queue_path(tmp_path, _ACCOUNT_ONE) != _queue_path(tmp_path, _ACCOUNT_TWO)


def test_due_and_upcoming_lists_are_local_and_exclude_rejected_packets(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    due = _create(
        store, text="Share a clear hiring timeline with every candidate.", scheduled_at=_NOW
    ).packet
    upcoming = _create(
        store,
        text="Give candidates a concise role brief before the first interview.",
        scheduled_at=_NOW + timedelta(days=1),
    ).packet
    rejected = _create(
        store,
        text="Explain interview steps before a candidate applies.",
        scheduled_at=_NOW - timedelta(days=1),
    ).packet
    store.reject(_ACCOUNT_ONE, rejected.packet_id, reason="Needs a fresh editorial review.")

    assert tuple(packet.packet_id for packet in store.due(_ACCOUNT_ONE)) == (due.packet_id,)
    assert tuple(packet.packet_id for packet in store.upcoming(_ACCOUNT_ONE)) == (
        upcoming.packet_id,
    )
    clock.value += timedelta(days=2)
    assert {packet.packet_id for packet in store.due(_ACCOUNT_ONE)} == {
        due.packet_id,
        upcoming.packet_id,
    }
    assert store.upcoming(_ACCOUNT_ONE) == ()


def test_exact_duplicate_returns_existing_id_and_near_duplicate_warns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _create(store).packet
    exact = _create(
        store,
        text="  STRONG recruitment starts with a clear role brief, a predictable interview timeline, "
        "and a useful update after each conversation!  ",
        scheduled_at=_NOW + timedelta(days=2),
    )
    near_text = (
        "Strong recruitment starts with a clear role brief, a predictable interview timeline, "
        "and a useful update after every conversation."
    )
    near = _create(store, text=near_text, scheduled_at=_NOW + timedelta(days=3))

    assert exact.created is False
    assert exact.packet.packet_id == first.packet_id
    assert exact.warnings[0].code == "EXACT_DUPLICATE"
    assert near.created is True
    assert near.warnings[0].code == "NEAR_DUPLICATE"
    assert near.warnings[0].matched_packet_id == first.packet_id
    assert near.warnings[0].similarity_percent >= 86
    assert len(store.list(_ACCOUNT_ONE).packets) == 2


@pytest.mark.parametrize(
    ("provenance", "rights", "consent", "expected_code"),
    [
        (None, True, True, "CONTENT_PLANNER_SOURCE_INVALID"),
        ("EXTERNAL_SOURCE", True, True, "CONTENT_PLANNER_SOURCE_INVALID"),
        ("OPERATOR_AUTHORED", False, True, "CONTENT_PLANNER_RIGHTS_REQUIRED"),
        ("OPERATOR_AUTHORED", True, False, "CONTENT_PLANNER_RIGHTS_REQUIRED"),
        ("OPERATOR_AUTHORED", True, None, "CONTENT_PLANNER_RIGHTS_REQUIRED"),
    ],
)
def test_invalid_source_and_missing_rights_or_consent_fail_closed(
    tmp_path: Path,
    provenance: object,
    rights: object,
    consent: object,
    expected_code: str,
) -> None:
    with pytest.raises(RecruitmentContentPlannerError) as error:
        _store(tmp_path).create(
            _ACCOUNT_ONE,
            _TEXT,
            _NOW + timedelta(days=1),
            provenance=provenance,
            rights_asserted=rights,
            consent_asserted=consent,
        )
    assert error.value.code == expected_code
    assert list(tmp_path.glob(f"{_NAMESPACE}/**/*.json")) == []


def test_attachment_with_unknown_rights_or_local_url_is_rejected(tmp_path: Path) -> None:
    missing_rights = ContentAttachmentMetadataV1(
        kind="VIDEO",
        url="https://example.com/video.mp4",
    )
    with pytest.raises(RecruitmentContentPlannerError) as rights_error:
        _create(_store(tmp_path), attachment=missing_rights)
    assert rights_error.value.code == "CONTENT_PLANNER_RIGHTS_REQUIRED"

    local_url = ContentAttachmentMetadataV1(
        kind="REFERENCE_LINK",
        url="https://localhost/private",
        rights_asserted=True,
        consent_asserted=True,
    )
    with pytest.raises(RecruitmentContentPlannerError) as url_error:
        _create(_store(tmp_path), attachment=local_url)
    assert url_error.value.code == "CONTENT_PLANNER_INVALID"


@pytest.mark.parametrize(
    "text",
    [
        "Candidate CV: Casey Example\nEmail: casey@example.invalid\nPhone: +1 415 555 0132",
        "The applicant's resume includes private employment history.",
        "Date of birth: 1990-02-03",
        "Call this applicant at 415-555-0132 to discuss the role.",
        "The candidate's home address is 123 Main Street.",
        "Candidate application details are at https://example.invalid/private/profile.",
    ],
)
def test_private_cv_and_personal_data_are_rejected_without_echoing_content(
    tmp_path: Path,
    text: str,
) -> None:
    with pytest.raises(RecruitmentContentPlannerError) as error:
        _create(_store(tmp_path), text=text)
    assert error.value.code == "CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED"
    assert text not in str(error.value)
    assert list(tmp_path.glob(f"{_NAMESPACE}/**/*.json")) == []


def test_private_notes_and_attachment_labels_are_screened(tmp_path: Path) -> None:
    with pytest.raises(RecruitmentContentPlannerError) as note_error:
        _create(_store(tmp_path), private_notes="Contact person@example.invalid for details.")
    assert note_error.value.code == "CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED"

    attachment = ContentAttachmentMetadataV1(
        kind="IMAGE",
        url="https://example.com/photo.jpg",
        label="Candidate CV scan",
        rights_asserted=True,
        consent_asserted=True,
    )
    with pytest.raises(RecruitmentContentPlannerError) as attachment_error:
        _create(_store(tmp_path), attachment=attachment)
    assert attachment_error.value.code == "CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED"


def test_utc_schedule_bounds_and_fixed_precision_are_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RecruitmentContentPlannerError) as naive:
        _create(store, scheduled_at=datetime(2026, 10, 13, 9, 30))
    assert naive.value.code == "CONTENT_PLANNER_SCHEDULE_INVALID"
    with pytest.raises(RecruitmentContentPlannerError) as offset:
        _create(
            store,
            scheduled_at=datetime(2026, 10, 13, 14, 30, tzinfo=timezone(timedelta(hours=5))),
        )
    assert offset.value.code == "CONTENT_PLANNER_SCHEDULE_INVALID"
    with pytest.raises(RecruitmentContentPlannerError) as too_early:
        _create(store, scheduled_at=_NOW - timedelta(days=180, seconds=1))
    assert too_early.value.code == "CONTENT_PLANNER_SCHEDULE_INVALID"
    with pytest.raises(RecruitmentContentPlannerError) as too_late:
        _create(store, scheduled_at=_NOW + timedelta(days=180, seconds=1))
    assert too_late.value.code == "CONTENT_PLANNER_SCHEDULE_INVALID"

    boundary = _create(
        store,
        text="A concise recruitment reminder can set clear interview expectations.",
        scheduled_at=_NOW + timedelta(days=180),
    ).packet
    assert boundary.scheduled_at == _NOW + timedelta(days=180)
    assert boundary.scheduled_at.utcoffset() == timedelta(0)


def test_pending_and_retained_limits_are_account_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = [_create(store, text=_unique_text(index)).packet for index in range(20)]
    assert store.list(_ACCOUNT_ONE).active_count == 20
    with pytest.raises(RecruitmentContentPlannerError) as active_limit:
        _create(store, text=_unique_text(20))
    assert active_limit.value.code == "CONTENT_PLANNER_LIMIT_REACHED"

    for packet in created:
        store.reject(_ACCOUNT_ONE, packet.packet_id, reason="Editorial calendar reset.")
    second_batch = [
        _create(store, text=f"Second calendar item {index}: explain hiring steps clearly.").packet
        for index in range(20)
    ]
    assert len(store.list(_ACCOUNT_ONE).packets) == 40
    assert store.list(_ACCOUNT_ONE).active_count == 20
    store.reject(_ACCOUNT_ONE, second_batch[0].packet_id, reason="No longer planned.")
    with pytest.raises(RecruitmentContentPlannerError) as retained_limit:
        _create(store, text="One more distinct item: share a transparent hiring timeline.")
    assert retained_limit.value.code == "CONTENT_PLANNER_LIMIT_REACHED"

    other = _create(_store(tmp_path), _ACCOUNT_TWO, text=_unique_text(20)).packet
    assert other.account_uuid == _ACCOUNT_TWO
    assert store.list(_ACCOUNT_ONE).active_count == 19


def test_restart_preserves_packet_ids_review_state_and_safe_repr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    packet = _create(store, private_notes="Private editorial thought.").packet
    approved = store.approve(
        _ACCOUNT_ONE, packet.packet_id, review_note="Approved for manual review."
    )
    restarted = _store(tmp_path)
    loaded = restarted.get(_ACCOUNT_ONE, packet.packet_id)

    assert loaded == approved
    assert loaded.packet_id == packet.packet_id
    assert loaded.editorial_status == "READY_FOR_MANUAL_PUBLICATION"
    assert loaded.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert "Private editorial thought" not in repr(loaded)
    assert "Strong recruitment starts" not in repr(loaded)


def test_reject_is_explicit_terminal_editorial_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    rejected = store.reject(
        _ACCOUNT_ONE, packet.packet_id, reason="Rights documentation is incomplete."
    )
    assert rejected.editorial_status == "REJECTED"
    assert rejected.review_note == "Rights documentation is incomplete."
    assert rejected.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    with pytest.raises(RecruitmentContentPlannerError) as error:
        store.approve(_ACCOUNT_ONE, packet.packet_id)
    assert error.value.code == "CONTENT_PLANNER_REVIEW_INVALID"


def test_review_snapshot_is_immutable_versioned_and_account_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_packet = _create(
        store,
        private_notes="Keep the interview timeline specific.",
        attachment=ContentAttachmentMetadataV1(
            kind="IMAGE",
            url="https://example.com/recruitment-card.png",
            label="Recruitment card",
            attribution="Created for this campaign",
            rights_asserted=True,
            consent_asserted=True,
        ),
    ).packet
    first = store.get_review_snapshot(_ACCOUNT_ONE, first_packet.packet_id)
    other_packet = _create(_store(tmp_path), _ACCOUNT_TWO).packet
    other = store.get_review_snapshot(_ACCOUNT_TWO, other_packet.packet_id)

    assert first.packet == first_packet
    assert first.fingerprint.startswith("content-packet-v1:sha256:")
    assert len(first.fingerprint.rsplit(":", maxsplit=1)[-1]) == 64
    assert first.fingerprint != other.fingerprint
    assert first.packet.account_uuid == _ACCOUNT_ONE
    assert first.packet.attachment is not None
    assert "Strong recruitment starts" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.fingerprint = other.fingerprint  # type: ignore[misc]


@pytest.mark.parametrize(
    ("decision", "note", "expected_status"),
    [
        ("APPROVE", "Editorial check complete.", "READY_FOR_MANUAL_PUBLICATION"),
        ("REJECT", "Needs a different recruitment focus.", "REJECTED"),
    ],
)
def test_review_if_unchanged_applies_confirmed_decision_without_remote_publication(
    tmp_path: Path,
    decision: str,
    note: str,
    expected_status: str,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)

    reviewed = store.review_if_unchanged(
        _ACCOUNT_ONE,
        packet.packet_id,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision=decision,  # type: ignore[arg-type]
        note=note,
    )

    assert reviewed.editorial_status == expected_status
    assert reviewed.review_note == note
    assert reviewed.reviewed_at == _NOW
    assert reviewed.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id).fingerprint != (
        snapshot.fingerprint
    )


@pytest.mark.parametrize(
    ("changes", "with_attachment"),
    [
        ({"original_text": _TEXT + " A changed suffix."}, False),
        ({"scheduled_at": "2026-10-15T09:30:00.000000Z"}, False),
        ({"created_at": "2026-10-11T09:30:00.000000Z"}, False),
        ({"updated_at": "2026-10-12T09:31:00.000000Z"}, False),
        ({"private_notes": "A changed private planning note."}, False),
        ({"rights_asserted": False}, False),
        ({"consent_asserted": False}, False),
        (
            {
                "editorial_status": "READY_FOR_MANUAL_PUBLICATION",
                "updated_at": "2026-10-12T09:31:00.000000Z",
                "reviewed_at": "2026-10-12T09:31:00.000000Z",
            },
            False,
        ),
        (
            {
                "attachment": {
                    "kind": "IMAGE",
                    "url": "https://example.com/updated-recruitment-card.png",
                    "label": "Updated card",
                    "attribution": "Created for this campaign",
                    "rights_asserted": True,
                    "consent_asserted": True,
                    "rights_assertion_status": "UNVERIFIED_OPERATOR_ASSERTION",
                }
            },
            True,
        ),
    ],
)
def test_packet_changes_after_snapshot_fail_stale_without_writing(
    tmp_path: Path,
    changes: dict[str, object],
    with_attachment: bool,
) -> None:
    store = _store(tmp_path)
    attachment = (
        ContentAttachmentMetadataV1(
            kind="IMAGE",
            url="https://example.com/recruitment-card.png",
            label="Recruitment card",
            attribution="Created for this campaign",
            rights_asserted=True,
            consent_asserted=True,
        )
        if with_attachment
        else None
    )
    packet = _create(
        store,
        attachment=attachment,
        private_notes="Original private planning note.",
    ).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    current_bytes = _replace_packet_fields(tmp_path, changes)

    with pytest.raises(RecruitmentContentPlannerError) as stale:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )

    assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert current_bytes == _queue_path(tmp_path).read_bytes()


@pytest.mark.parametrize(
    ("first_decision", "second_decision", "second_note"),
    [
        ("APPROVE", "REJECT", "Competing editorial decision."),
        ("REJECT", "APPROVE", None),
    ],
)
def test_competing_editorial_decisions_use_one_snapshot_and_reject_loser(
    tmp_path: Path,
    first_decision: str,
    second_decision: str,
    second_note: str | None,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    first_note = "First editorial decision." if first_decision == "REJECT" else None

    store.review_if_unchanged(
        _ACCOUNT_ONE,
        packet.packet_id,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision=first_decision,  # type: ignore[arg-type]
        note=first_note,
    )
    after_winner = _queue_path(tmp_path).read_bytes()
    with pytest.raises(RecruitmentContentPlannerError) as stale:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision=second_decision,  # type: ignore[arg-type]
            note=second_note,
        )

    assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert _queue_path(tmp_path).read_bytes() == after_winner


def test_repeated_stale_approval_and_wrong_account_or_packet_never_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    original = _queue_path(tmp_path).read_bytes()

    for account_id, packet_id in (
        (_ACCOUNT_TWO, packet.packet_id),
        (_ACCOUNT_ONE, UUID("33333333-3333-4333-8333-333333333333")),
    ):
        with pytest.raises(RecruitmentContentPlannerError) as stale:
            store.review_if_unchanged(
                account_id,
                packet_id,
                expected_fingerprint=snapshot.fingerprint,
                expected_editorial_status="DRAFT",
                decision="APPROVE",
            )
        assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
        assert _queue_path(tmp_path).read_bytes() == original

    store.review_if_unchanged(
        _ACCOUNT_ONE,
        packet.packet_id,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision="APPROVE",
    )
    approved_bytes = _queue_path(tmp_path).read_bytes()
    for _ in range(2):
        with pytest.raises(RecruitmentContentPlannerError) as stale:
            store.review_if_unchanged(
                _ACCOUNT_ONE,
                packet.packet_id,
                expected_fingerprint=snapshot.fingerprint,
                expected_editorial_status="DRAFT",
                decision="APPROVE",
            )
        assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
        assert _queue_path(tmp_path).read_bytes() == approved_bytes


def test_review_if_unchanged_requires_expected_draft_and_valid_fingerprint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    original = _queue_path(tmp_path).read_bytes()

    with pytest.raises(RecruitmentContentPlannerError) as invalid:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint="not-a-review-fingerprint",
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )
    assert invalid.value.code == "CONTENT_PLANNER_INVALID"

    with pytest.raises(RecruitmentContentPlannerError) as stale:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="READY_FOR_MANUAL_PUBLICATION",
            decision="APPROVE",
        )
    assert stale.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert _queue_path(tmp_path).read_bytes() == original


def test_review_if_unchanged_lock_contention_does_not_reenter_lock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    original = _queue_path(tmp_path).read_bytes()

    with (
        store.account_lock(_ACCOUNT_ONE),
        pytest.raises(RecruitmentContentPlannerError) as busy,
    ):
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )

    assert busy.value.code == "CONTENT_PLANNER_BUSY"
    assert _queue_path(tmp_path).read_bytes() == original


def test_review_if_unchanged_write_failure_preserves_bytes_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    original = _queue_path(tmp_path).read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected local write failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RecruitmentContentPlannerError) as error:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )

    assert error.value.code == "CONTENT_PLANNER_STATE_INVALID"
    assert _queue_path(tmp_path).read_bytes() == original
    assert not list(_queue_path(tmp_path).parent.glob(".*.tmp"))
    monkeypatch.undo()

    reviewed = store.review_if_unchanged(
        _ACCOUNT_ONE,
        packet.packet_id,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision="APPROVE",
    )
    assert reviewed.editorial_status == "READY_FOR_MANUAL_PUBLICATION"


def test_review_if_unchanged_fails_closed_on_malformed_or_symlinked_current_file(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    target = _queue_path(tmp_path)
    target.write_bytes(b"not valid JSON")
    if os.name != "nt":
        target.chmod(0o600)
    malformed_bytes = target.read_bytes()

    with pytest.raises(RecruitmentContentPlannerError) as malformed:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )
    assert malformed.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert target.read_bytes() == malformed_bytes

    outside = tmp_path / "outside-planner.json"
    outside.write_text("{}", encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("file symlink creation is unavailable on this host")
    outside_bytes = outside.read_bytes()

    with pytest.raises(RecruitmentContentPlannerError) as symlinked:
        store.review_if_unchanged(
            _ACCOUNT_ONE,
            packet.packet_id,
            expected_fingerprint=snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )
    assert symlinked.value.code == "CONTENT_PLANNER_REVIEW_STALE"
    assert outside.read_bytes() == outside_bytes


@pytest.mark.parametrize(
    "case",
    ["unknown_key", "bad_version", "wrong_account", "published"],
)
def test_tampered_unknown_version_account_and_published_status_fail_closed(
    tmp_path: Path,
    case: str,
) -> None:
    store = _store(tmp_path)
    _create(store)
    path = _queue_path(tmp_path)
    document = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    if case == "unknown_key":
        document["extra"] = True
    elif case == "bad_version":
        document["version"] = True
    elif case == "wrong_account":
        document["account_uuid"] = str(_ACCOUNT_TWO)
    else:
        packets = cast(list[object], document["packets"])
        packet = cast(dict[str, object], packets[0])
        document["packets"] = [{**packet, "editorial_status": "PUBLISHED"}]
    _write_private_json(path, document)
    with pytest.raises(RecruitmentContentPlannerError):
        store.list(_ACCOUNT_ONE)


def test_duplicate_keys_invalid_utf8_and_oversized_documents_reject(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    path = _queue_path(tmp_path)
    duplicate = (
        f'{{"version":1,"version":1,"account_uuid":"{_ACCOUNT_ONE}","packets":[]}}'
    ).encode()
    path.write_bytes(duplicate)
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(RecruitmentContentPlannerError) as duplicate_error:
        store.list(_ACCOUNT_ONE)
    assert duplicate_error.value.code == "CONTENT_PLANNER_STATE_INVALID"

    path.write_bytes(b"\xff")
    with pytest.raises(RecruitmentContentPlannerError):
        store.list(_ACCOUNT_ONE)

    path.write_bytes(b" " * (512 * 1024 + 1))
    with pytest.raises(RecruitmentContentPlannerError):
        store.list(_ACCOUNT_ONE)


def test_symlinked_planner_namespace_and_account_file_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-planner-outside"
    outside.mkdir()
    namespace = tmp_path / _NAMESPACE
    try:
        namespace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    with pytest.raises(RecruitmentContentPlannerError) as directory_error:
        _store(tmp_path).list(_ACCOUNT_ONE)
    assert directory_error.value.code == "CONTENT_PLANNER_STATE_INVALID"
    namespace.unlink()

    store = _store(tmp_path)
    _create(store)
    target = _queue_path(tmp_path)
    target.unlink()
    secret_target = tmp_path / "outside.json"
    secret_target.write_text("{}", encoding="utf-8")
    try:
        target.symlink_to(secret_target)
    except OSError:
        pytest.skip("file symlink creation is unavailable on this host")
    with pytest.raises(RecruitmentContentPlannerError) as file_error:
        store.list(_ACCOUNT_ONE)
    assert file_error.value.code == "CONTENT_PLANNER_STATE_INVALID"


def test_atomic_write_failure_keeps_existing_document_and_restart_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    packet = _create(store).packet
    path = _queue_path(tmp_path)
    original = path.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected local write failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RecruitmentContentPlannerError) as error:
        store.approve(_ACCOUNT_ONE, packet.packet_id)
    assert error.value.code == "CONTENT_PLANNER_STATE_INVALID"
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.tmp"))

    monkeypatch.undo()
    assert _store(tmp_path).get(_ACCOUNT_ONE, packet.packet_id).editorial_status == "DRAFT"


def test_s4_account_lock_contention_is_reported_safely(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with (
        store.account_lock(_ACCOUNT_ONE),
        pytest.raises(RecruitmentContentPlannerError) as error,
    ):
        store.list(_ACCOUNT_ONE)
    assert error.value.code == "CONTENT_PLANNER_BUSY"


def test_store_files_are_private_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not enforced on Windows")
    packet = _create(_store(tmp_path)).packet
    path = _queue_path(tmp_path)
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    assert packet.packet_id == _store(tmp_path).list(_ACCOUNT_ONE).packets[0].packet_id


def test_local_planner_has_no_network_process_or_publish_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected external side effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    store = _store(tmp_path)
    packet = _create(store).packet
    snapshot = store.get_review_snapshot(_ACCOUNT_ONE, packet.packet_id)
    approved = store.review_if_unchanged(
        _ACCOUNT_ONE,
        packet.packet_id,
        expected_fingerprint=snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision="APPROVE",
    )
    assert store.list(_ACCOUNT_ONE).packets[0] == approved
    assert len(store.due(_ACCOUNT_ONE)) == 0
    assert len(store.upcoming(_ACCOUNT_ONE)) == 1
    assert approved.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert not hasattr(store, "publish")
