from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from threads_platform.browser import job_observation as observation_module
from threads_platform.browser.job_observation import (
    FakeBodyVisibility,
    FakeJobCardV1,
    FakeJobFeedBatchV1,
    FakeJobPostDetailV1,
    FakePageState,
    ObservationReasonCode,
    ObservationRunStatus,
    ObservedPostKind,
    PostObservationStatus,
    observe_fake_job_feed,
    to_parser_observation,
)
from threads_platform.standalone import recruitment_job_store as store_module
from threads_platform.standalone.recruitment_job_store import (
    MAX_ACCOUNT_ARCHIVE_BYTES,
    MAX_POSTS_PER_ACCOUNT,
    MAX_TEXT_CODEPOINTS,
    ExportField,
    RecruitmentJobStore,
    RecruitmentJobStoreError,
    RetentionPolicy,
    StoreErrorCode,
)
from threads_platform.standalone.recruitment_job_vacancy import (
    EvidenceState,
    ExtractionResult,
    extract_vietnamese_vacancies,
)

_ACCOUNT_ONE = UUID("00000000-0000-4000-8000-000000000101")
_ACCOUNT_TWO = UUID("00000000-0000-4000-8000-000000000102")
_BASE = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
_POLICY = RetentionPolicy(raw_text_days=30, derived_vacancy_days=90)
_EXPORT_FIELDS = (
    ExportField.POST_IDENTITY_SHA256,
    ExportField.OBSERVATION_ID,
    ExportField.POST_REVISION_ID,
    ExportField.VACANCY_ID,
    ExportField.VACANCY_REVISION_ID,
    ExportField.VACANCY_STATE,
    ExportField.OBSERVED_AT_UTC,
    ExportField.PUBLISHED_AT_UTC,
    ExportField.ROLE_TITLE,
    ExportField.SALARY_MIN,
    ExportField.SALARY_MAX,
)


def _url(index: int = 1) -> str:
    return f"https://threads.example.invalid/@synthetic/post-{index:03d}"


def _revision_id(account_id: UUID, target: str, body: str) -> str:
    digest = hashlib.sha256(
        "\0".join((str(account_id), target, body)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"bj02-rev-{digest[:32]}"


def _observation(
    *,
    observation_id: str = "synthetic-obs-001",
    account_id: UUID = _ACCOUNT_ONE,
    target: str | None = None,
    body: str | None = "Cần tuyển sales tại Hà Nội; lương thỏa thuận",
    observed_at: datetime = _BASE,
    published_at: datetime | None = _BASE - timedelta(days=1),
    visibility: FakeBodyVisibility | None = None,
    status: PostObservationStatus | None = None,
    truncated: bool = False,
    post_kind: ObservedPostKind = ObservedPostKind.ORIGINAL,
    author_label: str | None = "synthetic-employer-001",
    quoted_url: str | None = None,
    quoted_author: str | None = None,
    parent_url: str | None = None,
    parent_author: str | None = None,
    reason_codes: tuple[ObservationReasonCode, ...] | None = None,
) -> observation_module.BrowserJobObservationV1:
    canonical_url = target or _url()
    if visibility is None:
        visibility = (
            FakeBodyVisibility.UNKNOWN
            if body is None
            else FakeBodyVisibility.PARTIAL
            if truncated
            else FakeBodyVisibility.FULL_VISIBLE
        )
    if status is None:
        status = (
            PostObservationStatus.UNKNOWN
            if body is None
            or visibility is FakeBodyVisibility.UNKNOWN
            or post_kind is ObservedPostKind.UNKNOWN
            else PostObservationStatus.PARTIAL
            if truncated or visibility is FakeBodyVisibility.PARTIAL
            else PostObservationStatus.COMPLETE
        )
    if reason_codes is None:
        reason_codes = (
            (ObservationReasonCode.IMAGE_ONLY,)
            if body is None
            else (ObservationReasonCode.TEXT_PARTIAL,)
            if truncated or visibility is FakeBodyVisibility.PARTIAL
            else ()
        )
    return observation_module.BrowserJobObservationV1(
        schema_version=1,
        observation_id=observation_id,
        account_uuid=account_id,
        canonical_post_url=canonical_url,
        local_card_id="card-synthetic-001",
        observed_at_utc=observed_at,
        post_revision_id=None if body is None else _revision_id(account_id, canonical_url, body),
        source="SYNTHETIC_FIXTURE",
        text_provenance="SYNTHETIC_FIXTURE_TEXT",
        completeness_basis="FIXTURE_DECLARED",
        author_marker="UNVERIFIED",
        author_label=author_label,
        visible_text=body,
        text_completeness=visibility,
        text_truncated=truncated,
        status=status,
        reason_codes=reason_codes,
        post_kind=post_kind,
        published_at_utc=published_at,
        quoted_post_url=quoted_url,
        quoted_author_label=quoted_author,
        parent_post_url=parent_url,
        parent_author_label=parent_author,
        related_author_marker=(
            "UNVERIFIED" if quoted_author is not None or parent_author is not None else None
        ),
    )


def _extract(
    observation: observation_module.BrowserJobObservationV1,
) -> ExtractionResult | None:
    if observation.visible_text is None:
        return None
    return extract_vietnamese_vacancies(to_parser_observation(observation))


def _store(tmp_path: Path, policy: RetentionPolicy = _POLICY) -> tuple[RecruitmentJobStore, Path]:
    root = tmp_path / "synthetic-vacancy-root"
    root.mkdir(mode=0o700)
    return RecruitmentJobStore(root, retention_policy=policy), root


def _record(
    store: RecruitmentJobStore,
    observation: observation_module.BrowserJobObservationV1,
    *,
    now_utc: datetime | None = None,
) -> object:
    return store.record_observation(
        observation,
        _extract(observation),
        now_utc=now_utc or observation.observed_at_utc,
    )


def _archive_path(root: Path, account_id: UUID = _ACCOUNT_ONE) -> Path:
    return root / "job_vacancies_v1" / "accounts" / f"{account_id}.json"


def _read_archive(root: Path, account_id: UUID = _ACCOUNT_ONE) -> dict[str, object]:
    return json.loads(_archive_path(root, account_id).read_text(encoding="utf-8"))


def _post(document: dict[str, object], index: int = 0) -> dict[str, object]:
    posts = document["posts"]
    assert isinstance(posts, list)
    return posts[index]


def _events(post: dict[str, object]) -> list[dict[str, object]]:
    events = post["observations"]
    assert isinstance(events, list)
    return events


def _revisions(post: dict[str, object]) -> list[dict[str, object]]:
    revisions = post["revisions"]
    assert isinstance(revisions, list)
    return revisions


def _vacancy_states(post: dict[str, object]) -> list[dict[str, object]]:
    states = post["vacancy_states"]
    assert isinstance(states, list)
    return states


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.1
        return self.value


class _StopSignal:
    @staticmethod
    def is_set() -> bool:
        return False


class _FakeSession:
    def __init__(
        self,
        target: str,
        body: str,
        account_id: UUID = _ACCOUNT_ONE,
    ) -> None:
        self.target = target
        self.account_id = account_id
        self.body = body
        self.opened: list[str] = []

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> observation_module.FakeJobFeedBatchV1:
        assert account_uuid == self.account_id
        assert query == "synthetic vacancies"
        assert max_cards >= 1
        return FakeJobFeedBatchV1(
            batch_index=0,
            account_uuid=account_uuid,
            query=query,
            page_url=observation_module.SYNTHETIC_SEARCH_URL,
            state=FakePageState.READY,
            cards=(
                FakeJobCardV1(
                    account_uuid=account_uuid,
                    local_card_id="card-synthetic-001",
                    canonical_post_url=self.target,
                    author_marker="synthetic-employer-001",
                ),
            ),
            has_more=False,
        )

    def scroll_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        next_batch_index: int,
        increment: int,
        max_cards: int,
    ) -> observation_module.FakeJobFeedBatchV1:
        raise AssertionError("no scroll expected")

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> observation_module.FakeJobPostDetailV1:
        self.opened.append(canonical_post_url)
        assert account_uuid == self.account_id
        assert canonical_post_url == self.target
        return FakeJobPostDetailV1(
            requested_url=self.target,
            page_url=self.target,
            canonical_post_url=self.target,
            account_uuid=account_uuid,
            state=FakePageState.READY,
            author_marker="synthetic-employer-001",
            body=self.body,
            body_visibility=FakeBodyVisibility.FULL_VISIBLE,
            post_kind=ObservedPostKind.ORIGINAL,
            published_at_utc=_BASE - timedelta(days=1),
        )


def test_fake_observation_parser_store_bridge_preserves_evidence_and_redacts_contacts(
    tmp_path: Path,
) -> None:
    target = _url()
    body = (
        "Cần tuyển sales tại Hà Nội; lương thỏa thuận; "
        "SĐT: [SYNTHETIC_PHONE_001]; email: fake@example.invalid"
    )
    session = _FakeSession(target, body)
    run = observe_fake_job_feed(
        session,
        account_uuid=_ACCOUNT_ONE,
        query="synthetic vacancies",
        selected_targets=(target,),
        clock=_Clock(),
        stop_signal=_StopSignal(),
        capture_id="capture-synthetic-001",
        observed_at_utc=_BASE,
    )
    assert run.status is ObservationRunStatus.COMPLETE
    assert session.opened == [target]
    observation = run.observations[0]
    parser_input = to_parser_observation(observation)
    extraction = extract_vietnamese_vacancies(parser_input)
    assert extraction.records

    store, root = _store(tmp_path)
    receipt = store.record_observation(observation, extraction, now_utc=_BASE)
    assert receipt.status == "STORED"
    assert store.summary(_ACCOUNT_ONE).observation_count == 1

    document = _read_archive(root)
    post = _post(document)
    assert post["canonical_post_url"] == target
    assert post["source_rights_metadata"] == "UNVERIFIED_SYNTHETIC_DECLARATION"
    revision = _revisions(post)[0]
    assert revision["observed_text"] == body
    vacancy_revision = revision["vacancy_revisions"][0]
    fields = vacancy_revision["fields"]
    assert fields["salary"]["raw"]["value"] == "lương thỏa thuận"
    assert fields["salary"]["amount_min"]["state"] == "ABSENT"
    assert fields["contacts"]["phone"] == {
        "state": "SOURCE_SPAN",
        "present": True,
        "evidence_count": 1,
    }
    assert "SYNTHETIC_PHONE_001" in revision["observed_text"]
    assert "fake@example.invalid" in revision["observed_text"]

    exported = store.export_json(
        _ACCOUNT_ONE,
        fields=_EXPORT_FIELDS,
        confirmed=True,
        now_utc=_BASE,
    )
    assert "SYNTHETIC_PHONE_001" not in exported
    assert "fake@example.invalid" not in exported
    assert "observed_text" not in exported
    assert "canonical_post_url" not in exported
    assert "contact_fields_included" in exported


def test_exact_permalink_identity_accounts_and_revision_history_are_separate(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    first = _observation(body="Cần tuyển sales tại Hà Nội")
    first_receipt = _record(store, first)
    duplicate = _record(store, first)
    repeated = _observation(
        observation_id="synthetic-obs-002",
        body="Cần tuyển sales tại Hà Nội",
        observed_at=_BASE + timedelta(minutes=1),
    )
    changed = _observation(
        observation_id="synthetic-obs-003",
        body="Cần tuyển sales tại Đà Nẵng",
        observed_at=_BASE + timedelta(minutes=2),
    )
    _record(store, repeated)
    _record(store, changed)

    assert first_receipt.status == "STORED"
    assert duplicate.status == "DUPLICATE"
    summary = store.summary(_ACCOUNT_ONE)
    assert summary.post_count == 1
    assert summary.observation_count == 3
    assert summary.revision_count == 2
    assert summary.vacancy_revision_count == 2

    post = _post(_read_archive(root))
    assert [_revision["post_revision_id"] for _revision in _revisions(post)] == [
        first.post_revision_id,
        changed.post_revision_id,
    ]
    assert len(_events(post)) == 3
    vacancy_states = _vacancy_states(post)
    assert len(vacancy_states) == 1
    assert vacancy_states[0]["state"] == "ACTIVE"
    assert vacancy_states[0]["last_observation_id"] == changed.observation_id

    same_target_other_account = _observation(
        observation_id="synthetic-obs-001",
        account_id=_ACCOUNT_TWO,
        target=_url(),
        body="Cần tuyển kế toán tại Hà Nội",
    )
    _record(store, same_target_other_account)
    assert store.summary(_ACCOUNT_TWO).post_count == 1
    assert store.summary(_ACCOUNT_TWO).observation_count == 1
    assert store.summary(_ACCOUNT_ONE).post_count == 1

    account_one_export = store.export_json(
        _ACCOUNT_ONE,
        fields=(ExportField.ROLE_TITLE,),
        confirmed=True,
        now_utc=changed.observed_at_utc,
    )
    account_two_export = store.export_json(
        _ACCOUNT_TWO,
        fields=(ExportField.ROLE_TITLE,),
        confirmed=True,
        now_utc=same_target_other_account.observed_at_utc,
    )
    assert "sales" in account_one_export.casefold()
    assert "kế toán" in account_two_export.casefold()
    assert "kế toán" not in account_one_export.casefold()

    different_case = _observation(
        observation_id="synthetic-obs-004",
        target=_url().replace("post-", "Post-"),
        body="Cần tuyển sales tại Hà Nội",
        observed_at=_BASE + timedelta(minutes=4),
    )
    _record(store, different_case)
    assert store.summary(_ACCOUNT_ONE).post_count == 2
    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(
            store,
            _observation(
                observation_id="synthetic-obs-005",
                target=_url() + "?utm_source=fixture",
                body="Cần tuyển sales",
            ),
        )
    assert error.value.code is StoreErrorCode.SOURCE_NOT_ALLOWED


def test_conflicting_observation_and_revision_ids_fail_closed(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    original = _observation()
    _record(store, original)

    same_id_different_event = _observation(
        observation_id=original.observation_id,
        observed_at=_BASE + timedelta(seconds=1),
    )
    with pytest.raises(RecruitmentJobStoreError) as event_error:
        _record(store, same_id_different_event)
    assert event_error.value.code is StoreErrorCode.OBSERVATION_CONFLICT

    changed_body_same_revision = replace(
        _observation(
            observation_id="synthetic-obs-010",
            body="Cần tuyển kế toán",
        ),
        post_revision_id=original.post_revision_id,
    )
    with pytest.raises(RecruitmentJobStoreError) as revision_error:
        _record(store, changed_body_same_revision)
    assert revision_error.value.code is StoreErrorCode.REVISION_CONFLICT
    assert store.summary(_ACCOUNT_ONE).observation_count == 1


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"source": "MANUAL_URL"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
        ({"source": "BROWSER_UI"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
        ({"text_provenance": "OPERATOR_SUPPLIED_TEXT"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
        ({"completeness_basis": "OPERATOR_ASSERTION"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
        ({"author_marker": "BROWSER_VISIBLE_VERIFIED"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
        (
            {"canonical_post_url": "https://www.threads.net/@real/post"},
            StoreErrorCode.SOURCE_NOT_ALLOWED,
        ),
        ({"author_label": "Real Candidate Name"}, StoreErrorCode.SOURCE_NOT_ALLOWED),
    ],
)
def test_unapproved_provenance_and_real_targets_are_rejected(
    tmp_path: Path,
    changes: dict[str, object],
    expected_code: StoreErrorCode,
) -> None:
    store, root = _store(tmp_path)
    observation = replace(_observation(), **changes)
    with pytest.raises(RecruitmentJobStoreError) as error:
        store.record_observation(observation, None, now_utc=_BASE)
    assert error.value.code is expected_code
    assert not (root / "job_vacancies_v1").exists()


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ("Cần tuyển sales; SĐT: 0901234567", StoreErrorCode.CONTACT_DATA_BLOCKED),
        ("Cần tuyển sales; Zalo: 000-000-0000", StoreErrorCode.CONTACT_DATA_BLOCKED),
        ("Cần tuyển sales; SĐT: [SYNTHETIC_PHONE_0000000000]", StoreErrorCode.CONTACT_DATA_BLOCKED),
        ("Cần tuyển sales; email: applicant@example.com", StoreErrorCode.CONTACT_DATA_BLOCKED),
        ("Cần tuyển sales; apply: https://real.example/jobs", StoreErrorCode.SOURCE_NOT_ALLOWED),
    ],
)
def test_synthetic_label_does_not_authorize_real_or_unverified_contacts(
    tmp_path: Path,
    body: str,
    expected_code: StoreErrorCode,
) -> None:
    store, root = _store(tmp_path)
    observation = _observation(body=body)
    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(store, observation)
    assert error.value.code is expected_code
    assert not (root / "job_vacancies_v1").exists()


def test_partial_quote_and_image_only_observations_keep_unknown_evidence(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    partial = _observation(
        observation_id="synthetic-obs-partial",
        body="Cần tuyển sales; lương 8tr",
        visibility=FakeBodyVisibility.PARTIAL,
        status=PostObservationStatus.PARTIAL,
        truncated=True,
        observed_at=_BASE,
    )
    quote = _observation(
        observation_id="synthetic-obs-quote",
        target=_url(2),
        body="Cần tuyển kế toán tại Hà Nội",
        post_kind=ObservedPostKind.QUOTE,
        quoted_url=_url(3),
        quoted_author="synthetic-quoted-author-003",
        observed_at=_BASE + timedelta(seconds=1),
    )
    image_only = _observation(
        observation_id="synthetic-obs-image",
        target=_url(3),
        body=None,
        observed_at=_BASE + timedelta(seconds=2),
    )
    _record(store, partial)
    _record(store, quote)
    _record(store, image_only)

    document = _read_archive(root)
    partial_post = next(
        post
        for post in document["posts"]
        if post["identity_sha256"] == store_module._identity_sha256(_ACCOUNT_ONE, _url())
    )
    partial_revision = _revisions(partial_post)[0]
    assert partial_revision["text_completeness"] == "PARTIAL"
    salary = partial_revision["vacancy_revisions"][0]["fields"]["salary"]["raw"]
    assert salary["state"] != "ABSENT"
    quote_post = next(
        post
        for post in document["posts"]
        if post["identity_sha256"] == store_module._identity_sha256(_ACCOUNT_ONE, _url(2))
    )
    assert _revisions(quote_post)[0]["vacancy_revisions"] == []
    image_post = next(
        post
        for post in document["posts"]
        if post["identity_sha256"] == store_module._identity_sha256(_ACCOUNT_ONE, _url(3))
    )
    assert image_post["revisions"] == []
    assert _events(image_post)[0]["text_completeness"] == "UNKNOWN"
    assert _events(image_post)[0]["published_at_utc"] == "2026-10-11T09:00:00Z"
    assert store.summary(_ACCOUNT_ONE).observation_count == 3


def test_absent_role_is_not_reobserved_only_after_a_complete_original_capture(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    sales = _observation(body="Cần tuyển sales")
    partial_accounting = _observation(
        observation_id="synthetic-obs-partial",
        body="Cần tuyển kế toán",
        visibility=FakeBodyVisibility.PARTIAL,
        status=PostObservationStatus.PARTIAL,
        truncated=True,
        observed_at=_BASE + timedelta(minutes=1),
    )
    complete_accounting = _observation(
        observation_id="synthetic-obs-complete",
        body="Cần tuyển kế toán, nhận hồ sơ trước ngày 30/11",
        observed_at=_BASE + timedelta(minutes=2),
    )
    _record(store, sales)
    _record(store, partial_accounting)
    post = _post(_read_archive(root))
    assert all(state["state"] == "ACTIVE" for state in _vacancy_states(post))
    _record(store, complete_accounting)
    post = _post(_read_archive(root))
    states = {state["role_anchor"]: state["state"] for state in _vacancy_states(post)}
    assert states["role-v1:sales"] == "NOT_REOBSERVED"
    assert states["role-v1:accounting"] == "ACTIVE"


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 10, 12, 9, 0),
        datetime(2026, 10, 12, 16, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_naive_and_non_utc_timestamps_are_rejected(tmp_path: Path, timestamp: datetime) -> None:
    store, root = _store(tmp_path)
    with pytest.raises(RecruitmentJobStoreError) as error:
        store.apply_retention(_ACCOUNT_ONE, now_utc=timestamp)
    assert error.value.code is StoreErrorCode.INVALID_INPUT
    assert not (root / "job_vacancies_v1").exists()


def test_non_utc_observation_and_future_capture_are_rejected(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    non_utc = _observation(
        observed_at=datetime(2026, 10, 12, 16, 0, tzinfo=timezone(timedelta(hours=7)))
    )
    with pytest.raises(RecruitmentJobStoreError) as timestamp_error:
        _record(store, non_utc, now_utc=_BASE)
    assert timestamp_error.value.code is StoreErrorCode.INVALID_INPUT

    future = _observation(observed_at=_BASE + timedelta(seconds=1))
    with pytest.raises(RecruitmentJobStoreError) as future_error:
        _record(store, future, now_utc=_BASE)
    assert future_error.value.code is StoreErrorCode.INVALID_INPUT
    assert not (root / "job_vacancies_v1").exists()


def test_observed_time_is_distinct_from_published_time(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    observation = _observation(
        observed_at=_BASE,
        published_at=_BASE - timedelta(days=3),
    )
    _record(store, observation)
    event = _events(_post(_read_archive(root)))[0]
    assert event["observed_at_utc"] == "2026-10-12T09:00:00Z"
    assert event["published_at_utc"] == "2026-10-09T09:00:00Z"


def test_unicode_source_spans_remain_exact_in_persisted_revision(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    body = "Cần tuyển sales tại Hà Nội; lương thỏa thuận"
    _record(store, _observation(body=body))
    revision = _revisions(_post(_read_archive(root)))[0]
    city = revision["vacancy_revisions"][0]["fields"]["location"]["province_city"]
    assert city["state"] == EvidenceState.SOURCE_SPAN.value
    start, end = city["spans"][0]
    assert body[start:end] == "Hà Nội"


def test_export_requires_confirmation_allowlist_and_denies_contact_opt_in(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation(body="Cần tuyển sales; SĐT: [SYNTHETIC_PHONE_001]"))
    assert _archive_path(root).exists()
    with pytest.raises(RecruitmentJobStoreError) as confirmation_error:
        store.export_json(
            _ACCOUNT_ONE,
            fields=(ExportField.ROLE_TITLE,),
            confirmed=False,
            now_utc=_BASE,
        )
    assert confirmation_error.value.code is StoreErrorCode.EXPORT_CONFIRMATION_REQUIRED

    for fields, include_contact in [
        ((ExportField.ROLE_TITLE,), True),
        (("phone",), False),
        (("contacts",), False),
    ]:
        with pytest.raises(RecruitmentJobStoreError) as contact_error:
            store.export_json(
                _ACCOUNT_ONE,
                fields=fields,
                confirmed=True,
                now_utc=_BASE,
                include_public_recruiter_contact=include_contact,
            )
        assert contact_error.value.code is StoreErrorCode.CONTACT_EXPORT_BLOCKED

    with pytest.raises(RecruitmentJobStoreError) as allowlist_error:
        store.export_json(
            _ACCOUNT_ONE,
            fields=("observed_text",),
            confirmed=True,
            now_utc=_BASE,
        )
    assert allowlist_error.value.code is StoreErrorCode.EXPORT_FIELD_INVALID


def test_csv_export_escapes_formula_prefixes_and_quotes_newlines() -> None:
    for value in (
        "=1+1",
        "+SUM(A1:A2)",
        "-2+3",
        "@SUM(A1)",
        " \t=1+1",
        "\r\n+SUM(A1)",
        "\x00-2+3",
    ):
        assert store_module._csv_cell(value).startswith("'")

    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerow(
        [store_module._csv_cell('=HYPERLINK("synthetic","line 1\nline 2")')]
    )
    parsed = next(csv.reader(io.StringIO(stream.getvalue(), newline="")))
    assert parsed == ['\'=HYPERLINK("synthetic","line 1\nline 2")']


def test_csv_export_round_trips_quoted_and_newline_values_as_text(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    body = 'Cần tuyển sales; yêu cầu: =HYPERLINK("x","line one")'
    _record(store, _observation(body=body))
    output = store.export_csv(
        _ACCOUNT_ONE,
        fields=(ExportField.REQUIREMENTS,),
        confirmed=True,
        now_utc=_BASE,
    )
    rows = list(csv.reader(output.splitlines()))
    assert rows[0] == [ExportField.REQUIREMENTS.value]
    assert len(rows) == 2
    assert rows[1][0].startswith("'=")
    assert "HYPERLINK" in rows[1][0]


def test_archive_size_and_entry_caps_accept_zero_one_max_and_reject_max_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module, "MAX_POSTS_PER_ACCOUNT", 3)
    monkeypatch.setattr(store_module, "MAX_OBSERVATIONS_PER_ACCOUNT", 3)
    monkeypatch.setattr(store_module, "MAX_OBSERVATIONS_PER_POST", 3)
    store, root = _store(tmp_path)
    assert store.summary(_ACCOUNT_ONE).post_count == 0

    for index in range(1, 4):
        _record(
            store,
            _observation(
                observation_id=f"synthetic-obs-{index:03d}",
                target=_url(index),
                body="Cần tuyển sales",
                observed_at=_BASE + timedelta(seconds=index),
            ),
        )
    summary = store.summary(_ACCOUNT_ONE)
    assert summary.post_count == 3
    assert summary.observation_count == 3
    assert store_module.MAX_POSTS_PER_ACCOUNT == 3

    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(
            store,
            _observation(
                observation_id="synthetic-obs-004",
                target=_url(4),
                body="Cần tuyển sales",
                observed_at=_BASE + timedelta(seconds=4),
            ),
        )
    assert error.value.code is StoreErrorCode.LIMIT_REACHED
    assert store.summary(_ACCOUNT_ONE).post_count == 3
    assert _archive_path(root).stat().st_size < MAX_ACCOUNT_ARCHIVE_BYTES


def test_hard_archive_entry_cap_is_bounded_at_two_hundred() -> None:
    assert MAX_POSTS_PER_ACCOUNT == 200
    assert store_module.MAX_OBSERVATIONS_PER_ACCOUNT == 200
    assert MAX_TEXT_CODEPOINTS == 10_000


def test_exact_text_cap_is_stored_and_cap_plus_one_is_rejected(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    exact_body = "Cần tuyển sales " + ("x" * (MAX_TEXT_CODEPOINTS - len("Cần tuyển sales ")))
    exact = _observation(body=exact_body)
    _record(store, exact)
    stored = _revisions(_post(_read_archive(root)))[0]
    assert len(stored["observed_text"]) == MAX_TEXT_CODEPOINTS

    over = _observation(
        observation_id="synthetic-obs-over",
        target=_url(2),
        body=exact_body + "x",
        observed_at=_BASE + timedelta(seconds=1),
    )
    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(store, over)
    assert error.value.code is StoreErrorCode.LIMIT_REACHED


def test_retention_purges_raw_evidence_then_derived_fields_deterministically(
    tmp_path: Path,
) -> None:
    policy = RetentionPolicy(raw_text_days=1, derived_vacancy_days=2)
    store, root = _store(tmp_path, policy)
    body = "Cần tuyển sales tại Hà Nội; lương thỏa thuận; SĐT: [SYNTHETIC_PHONE_001]"
    _record(store, _observation(body=body))

    raw_expiry = _BASE + timedelta(days=1)
    receipt = store.apply_retention(_ACCOUNT_ONE, now_utc=raw_expiry)
    assert receipt.status == "RETENTION_APPLIED"
    post = _post(_read_archive(root))
    revision = _revisions(post)[0]
    assert post["canonical_post_url"] is None
    assert revision["observed_text"] is None
    assert revision["retention_state"] == "RAW_PURGED"
    fields = revision["vacancy_revisions"][0]["fields"]
    city = fields["location"]["province_city"]
    assert city["spans"] == []
    assert city["raw_value"] is None
    assert city["state"] == "REDACTED_BY_RETENTION"
    assert fields["contacts"]["phone"]["present"] is False
    assert fields["role_title"]["value"] == "sales"

    raw_export = store.export_json(
        _ACCOUNT_ONE,
        fields=(ExportField.ROLE_TITLE,),
        confirmed=True,
        now_utc=raw_expiry,
    )
    assert "sales" in raw_export.casefold()
    assert "SYNTHETIC_PHONE_001" not in raw_export

    derived_expiry = _BASE + timedelta(days=2)
    store.apply_retention(_ACCOUNT_ONE, now_utc=derived_expiry)
    post = _post(_read_archive(root))
    revision = _revisions(post)[0]
    assert revision["retention_state"] == "DERIVED_PURGED"
    assert revision["vacancy_revisions"][0]["fields"] is None
    assert _vacancy_states(post)[0]["state"] == "EXPIRED"


def test_account_scoped_delete_uses_a_redacted_tombstone(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    target = _url()
    _record(store, _observation(account_id=_ACCOUNT_ONE, target=target, body="Cần tuyển sales"))
    _record(
        store,
        _observation(
            account_id=_ACCOUNT_TWO,
            target=target,
            observation_id="synthetic-obs-account-two",
            body="Cần tuyển kế toán",
        ),
    )
    deleted = store.delete_post(_ACCOUNT_ONE, target, now_utc=_BASE)
    assert deleted.status == "DELETED"
    account_one = _post(_read_archive(root, _ACCOUNT_ONE))
    assert account_one["deletion_state"] == "DELETED"
    assert account_one["canonical_post_url"] is None
    assert account_one["observations"] == []
    assert account_one["revisions"] == []
    assert store.summary(_ACCOUNT_ONE).deleted_post_count == 1
    assert store.summary(_ACCOUNT_TWO).active_post_count == 1
    account_two_export = store.export_json(
        _ACCOUNT_TWO,
        fields=(ExportField.ROLE_TITLE,),
        confirmed=True,
        now_utc=_BASE,
    )
    assert "kế toán" in account_two_export.casefold()
    with pytest.raises(RecruitmentJobStoreError) as deleted_error:
        _record(
            store,
            _observation(
                account_id=_ACCOUNT_ONE,
                target=target,
                observation_id="synthetic-obs-after-delete",
                body="Cần tuyển sales",
                observed_at=_BASE + timedelta(seconds=1),
            ),
        )
    assert deleted_error.value.code is StoreErrorCode.POST_DELETED
    with pytest.raises(RecruitmentJobStoreError) as missing_error:
        store.delete_post(_ACCOUNT_TWO, _url(99), now_utc=_BASE)
    assert missing_error.value.code is StoreErrorCode.NOT_FOUND
    assert (root / "job_vacancies_v1").exists()
    assert not (root / "manual_recruitment_v1").exists()


def test_nonregular_root_traversal_symlink_and_reparse_paths_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("synthetic", encoding="utf-8")
    store = RecruitmentJobStore(file_root, retention_policy=_POLICY)
    with pytest.raises(RecruitmentJobStoreError) as root_error:
        store.summary(_ACCOUNT_ONE)
    assert root_error.value.code is StoreErrorCode.ROOT_INVALID

    safe_store, root = _store(tmp_path)
    traversal = _observation(target="https://threads.example.invalid/@synthetic/../post")
    with pytest.raises(RecruitmentJobStoreError) as traversal_error:
        _record(safe_store, traversal)
    assert traversal_error.value.code is StoreErrorCode.SOURCE_NOT_ALLOWED

    symlink_root = tmp_path / "root-symlink"
    try:
        symlink_root.symlink_to(root, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("directory symlink creation is unavailable")
    symlink_store = RecruitmentJobStore(symlink_root, retention_policy=_POLICY)
    with pytest.raises(RecruitmentJobStoreError) as symlink_error:
        symlink_store.summary(_ACCOUNT_ONE)
    assert symlink_error.value.code is StoreErrorCode.ROOT_INVALID

    monkeypatch.setattr(store_module, "_is_reparse_point", lambda _info: True)
    with pytest.raises(RecruitmentJobStoreError) as reparse_error:
        safe_store.summary(_ACCOUNT_ONE)
    assert reparse_error.value.code is StoreErrorCode.ROOT_INVALID


def test_symlink_archive_and_nonregular_archive_fail_closed(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    archive = _archive_path(root)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    archive.unlink()
    try:
        archive.symlink_to(outside)
    except OSError, NotImplementedError:
        pytest.skip("file symlink creation is unavailable")
    with pytest.raises(RecruitmentJobStoreError) as symlink_error:
        store.summary(_ACCOUNT_ONE)
    assert symlink_error.value.code is StoreErrorCode.ROOT_INVALID

    archive.unlink()
    archive.mkdir()
    with pytest.raises(RecruitmentJobStoreError) as directory_error:
        store.summary(_ACCOUNT_ONE)
    assert directory_error.value.code is StoreErrorCode.ROOT_INVALID


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (b"\xff\xfe", StoreErrorCode.ARCHIVE_CORRUPT),
        (b"{bad json", StoreErrorCode.ARCHIVE_CORRUPT),
        (
            b'{"schema_version":1,"schema_version":1,"account_uuid":"00000000-0000-4000-8000-000000000101","posts":[]}',
            StoreErrorCode.ARCHIVE_CORRUPT,
        ),
    ],
)
def test_bad_utf8_json_and_duplicate_keys_fail_closed(
    tmp_path: Path,
    raw: bytes,
    expected_code: StoreErrorCode,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    _archive_path(root).write_bytes(raw)
    with pytest.raises(RecruitmentJobStoreError) as error:
        store.summary(_ACCOUNT_ONE)
    assert error.value.code is expected_code


@pytest.mark.parametrize("version", [0, 2, 99])
def test_old_and_new_archive_versions_are_not_migrated(
    tmp_path: Path,
    version: int,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    document = _read_archive(root)
    document["schema_version"] = version
    _archive_path(root).write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RecruitmentJobStoreError) as error:
        store.summary(_ACCOUNT_ONE)
    assert error.value.code is StoreErrorCode.VERSION_UNSUPPORTED


def test_wrong_account_archive_binding_and_nested_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    document = _read_archive(root)
    document["account_uuid"] = str(_ACCOUNT_TWO)
    _archive_path(root).write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RecruitmentJobStoreError) as account_error:
        store.summary(_ACCOUNT_ONE)
    assert account_error.value.code is StoreErrorCode.ACCOUNT_MISMATCH

    document["account_uuid"] = str(_ACCOUNT_ONE)
    document["posts"][0]["observations"][0]["source"] = "BROWSER_UI"
    _archive_path(root).write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RecruitmentJobStoreError) as nested_error:
        store.summary(_ACCOUNT_ONE)
    assert nested_error.value.code is StoreErrorCode.ARCHIVE_CORRUPT


def test_atomic_replace_failure_keeps_previous_archive_and_restart_ignores_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    archive = _archive_path(root)
    previous_bytes = archive.read_bytes()

    def fail_replace(_source: str | Path, _target: str | Path) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    second = _observation(
        observation_id="synthetic-obs-second",
        body="Cần tuyển kế toán",
        observed_at=_BASE + timedelta(minutes=1),
    )
    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(store, second)
    assert error.value.code is StoreErrorCode.PERSISTENCE_FAILED
    assert archive.read_bytes() == previous_bytes
    assert not list(archive.parent.glob(".archive-*.tmp"))
    monkeypatch.undo()

    stale_temp = archive.parent / ".archive-interrupted.tmp"
    stale_temp.write_bytes(b"interrupted synthetic write")
    restarted = RecruitmentJobStore(root, retention_policy=_POLICY)
    assert restarted.summary(_ACCOUNT_ONE).observation_count == 1
    assert archive.read_bytes() == previous_bytes
    assert stale_temp.exists()


def test_temporary_file_creation_failure_preserves_previous_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = _store(tmp_path)
    _record(store, _observation())
    archive = _archive_path(root)
    previous_bytes = archive.read_bytes()

    def fail_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store_module.tempfile, "mkstemp", fail_mkstemp)
    with pytest.raises(RecruitmentJobStoreError) as error:
        _record(
            store,
            _observation(
                observation_id="synthetic-obs-second",
                observed_at=_BASE + timedelta(minutes=1),
            ),
        )
    assert error.value.code is StoreErrorCode.PERSISTENCE_FAILED
    assert archive.read_bytes() == previous_bytes


def test_account_lock_is_nonblocking_and_account_specific(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    store.summary(_ACCOUNT_ONE)
    lock_path = root / "job_vacancies_v1" / "locks" / f"{_ACCOUNT_ONE}.lock"
    held_lock = store_module.FilesystemProcessLock(lock_path)
    held_lock.acquire()
    try:
        with pytest.raises(RecruitmentJobStoreError) as error:
            store.summary(_ACCOUNT_ONE)
        assert error.value.code is StoreErrorCode.BUSY
        assert store.summary(_ACCOUNT_TWO).post_count == 0
    finally:
        held_lock.release()


def test_contact_fields_never_appear_in_json_or_csv_and_repr_is_redacted(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    observation = _observation(
        body=(
            "Cần tuyển sales; SĐT: [SYNTHETIC_PHONE_001]; "
            "Zalo: [FAKE_CONTACT_002]; email: fake@example.invalid"
        )
    )
    receipt = _record(store, observation)
    output_json = store.export_json(
        _ACCOUNT_ONE,
        fields=(ExportField.ROLE_TITLE, ExportField.OBSERVATION_ID),
        confirmed=True,
        now_utc=_BASE,
    )
    output_csv = store.export_csv(
        _ACCOUNT_ONE,
        fields=(ExportField.ROLE_TITLE,),
        confirmed=True,
        now_utc=_BASE,
    )
    assert "SYNTHETIC_PHONE_001" not in output_json + output_csv
    assert "FAKE_CONTACT_002" not in output_json + output_csv
    assert "fake@example.invalid" not in output_json + output_csv
    assert "contact" not in repr(receipt).casefold()
    assert str(_ACCOUNT_ONE) not in repr(store.summary(_ACCOUNT_ONE))


def test_contact_embedded_in_requirement_is_withheld_from_json_and_csv_after_raw_expiry(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path, RetentionPolicy(raw_text_days=1, derived_vacancy_days=2))
    contact_placeholder = "[SYNTHETIC_PHONE_001]"
    _record(
        store,
        _observation(body=f"Cần tuyển sales yêu cầu: liên hệ {contact_placeholder}"),
    )

    post = _post(_read_archive(root))
    revision = _revisions(post)[0]
    requirement = revision["vacancy_revisions"][0]["fields"]["requirements"]
    assert requirement["state"] == EvidenceState.AMBIGUOUS.value
    assert requirement["value"] is None
    assert requirement["raw_value"] is None
    assert requirement["spans"] == []

    raw_expiry = _BASE + timedelta(days=1)
    output_json = store.export_json(
        _ACCOUNT_ONE,
        fields=(ExportField.REQUIREMENTS,),
        confirmed=True,
        now_utc=raw_expiry,
    )
    output_csv = store.export_csv(
        _ACCOUNT_ONE,
        fields=(ExportField.REQUIREMENTS,),
        confirmed=True,
        now_utc=raw_expiry,
    )
    assert json.loads(output_json)["records"][0][ExportField.REQUIREMENTS.value] is None
    assert list(csv.reader(io.StringIO(output_csv))) == [[ExportField.REQUIREMENTS.value], [""]]
    assert contact_placeholder not in output_json + output_csv

    retained_archive = _read_archive(root)
    retained_post = _post(retained_archive)
    retained_revision = _revisions(retained_post)[0]
    retained_requirement = retained_revision["vacancy_revisions"][0]["fields"]["requirements"]
    assert retained_revision["observed_text"] is None
    assert retained_requirement["state"] == EvidenceState.AMBIGUOUS.value
    assert contact_placeholder not in json.dumps(retained_archive, ensure_ascii=False)


def test_contact_in_multi_role_ambiguous_span_is_cleared_but_safe_spans_remain(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path, RetentionPolicy(raw_text_days=1, derived_vacancy_days=2))
    body = "Cần tuyển sales, kế toán; lương 8tr; yêu cầu: liên hệ [SYNTHETIC_PHONE_001]"
    observation = _observation(body=body)
    extraction = _extract(observation)
    assert extraction is not None
    assert len(extraction.records) == 2
    safe_salary_spans = [[span.start, span.end] for span in extraction.records[0].salary.raw.spans]
    assert safe_salary_spans
    assert all(
        "8tr" in body[start:end] or "8" == body[start:end] for start, end in safe_salary_spans
    )

    _record(store, observation)

    revision = _revisions(_post(_read_archive(root)))[0]
    vacancy_revisions = revision["vacancy_revisions"]
    assert isinstance(vacancy_revisions, list)
    assert len(vacancy_revisions) == 2
    for vacancy_revision in vacancy_revisions:
        assert isinstance(vacancy_revision, dict)
        fields = vacancy_revision["fields"]
        assert isinstance(fields, dict)
        requirement = fields["requirements"]
        assert isinstance(requirement, dict)
        assert requirement["state"] == EvidenceState.AMBIGUOUS.value
        assert requirement["value"] is None
        assert requirement["raw_value"] is None
        assert requirement["spans"] == []

        salary = fields["salary"]
        assert isinstance(salary, dict)
        salary_raw = salary["raw"]
        assert isinstance(salary_raw, dict)
        assert salary_raw["state"] == EvidenceState.AMBIGUOUS.value
        assert salary_raw["spans"] == safe_salary_spans
        assert all(
            "SYNTHETIC_PHONE_001" not in body[start:end] for start, end in salary_raw["spans"]
        )

    raw_expiry = _BASE + timedelta(days=1)
    output_json = store.export_json(
        _ACCOUNT_ONE,
        fields=(ExportField.REQUIREMENTS,),
        confirmed=True,
        now_utc=raw_expiry,
    )
    output_csv = store.export_csv(
        _ACCOUNT_ONE,
        fields=(ExportField.REQUIREMENTS,),
        confirmed=True,
        now_utc=raw_expiry,
    )
    exported_requirements = json.loads(output_json)["records"]
    assert len(exported_requirements) == 2
    assert all(record[ExportField.REQUIREMENTS.value] is None for record in exported_requirements)
    assert list(csv.reader(io.StringIO(output_csv))) == [
        [ExportField.REQUIREMENTS.value],
        [""],
        [""],
    ]
    assert "SYNTHETIC_PHONE_001" not in output_json + output_csv

    retained_archive = _read_archive(root)
    retained_revision = _revisions(_post(retained_archive))[0]
    assert retained_revision["observed_text"] is None
    assert "SYNTHETIC_PHONE_001" not in json.dumps(retained_archive, ensure_ascii=False)


def test_export_rejects_non_string_field_values_with_fixed_code(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    for export in (store.export_json, store.export_csv):
        with pytest.raises(RecruitmentJobStoreError) as error:
            export(
                _ACCOUNT_ONE,
                fields=(123,),  # type: ignore[arg-type]
                confirmed=True,
                now_utc=_BASE,
            )
        assert error.value.code is StoreErrorCode.EXPORT_FIELD_INVALID
    assert not (root / "job_vacancies_v1").exists()


def test_store_has_no_browser_network_api_or_existing_journal_imports() -> None:
    source = ast.parse(Path(store_module.__file__).read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(source) if isinstance(node, ast.ImportFrom)}
    imports.update(
        alias.name
        for node in ast.walk(source)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        module is not None
        and (
            module.startswith(("httpx", "playwright", "threads_platform.browser"))
            or module.endswith(("manual_outcomes", "opportunity_queue"))
        )
        for module in imports
    )
