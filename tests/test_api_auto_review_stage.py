from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from threads_platform.api.contracts import RemoteReply
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.standalone import api_auto_review_stage as stage_module
from threads_platform.standalone.api_auto_content import (
    ContentDecision,
    ContentOrigin,
    JobFactsDeclaration,
    PreparedContent,
    ReviewContentPacket,
    RightsBasis,
    RightsEvidence,
    prepare_review_packet,
)
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    ApiPermission,
    CampaignScope,
    CandidateSource,
    CandidateState,
    CapabilitySnapshot,
    ContextCompleteness,
    ObservationProvenance,
    ObservedCandidate,
    PlanDecision,
    QuotaSnapshot,
    ReviewIntent,
    TargetKind,
    VerifiedTargetBinding,
    plan_cycle,
)
from threads_platform.standalone.api_auto_review_context import (
    PrivateReviewDecision,
    PrivateReviewPreview,
    ReviewDisplaySnapshot,
    build_private_review_preview,
    review_display_context_sha256,
)
from threads_platform.standalone.api_auto_review_stage import (
    _MAX_STAGES_PER_ACCOUNT,
    PrivateReviewStageStore,
    ReviewStageDecision,
    ReviewStageReason,
    ReviewStageState,
    StagedReviewV1,
)

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
ACCOUNT_ID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_ACCOUNT_ID = UUID("87654321-4321-8765-4321-876543218765")
ACTOR_ID = "synthetic-actor-17"
ROOT_ID = "synthetic-root-31"
PARENT_ID = "synthetic-parent-32"
REPLY_ID = "synthetic-reply-33"
REPLY_TEXT = "Synthetic inbound reply asks a question?"
ROOT_TEXT = "Synthetic root text stays ephemeral."
PARENT_TEXT = "Synthetic parent text stays ephemeral."
CONTENT_TEXT = "Synthetic normalized operator text."
ATTRIBUTION = "Synthetic fixture attribution."
ALL_PERMISSIONS = frozenset(ApiPermission)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(
    *,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    now: datetime = NOW,
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    reply_usage: int = 1,
    reply_total: int = 100,
) -> CampaignScope:
    return CampaignScope(
        account_id=account_id,
        actor_id=actor_id,
        campaign_id="synthetic-review-stage",
        policy_sha256=_sha("synthetic-stage-policy"),
        capabilities=CapabilitySnapshot(
            account_id=account_id,
            actor_id=actor_id,
            permissions=permissions,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        quota=QuotaSnapshot(
            post_usage=1,
            post_total=100,
            reply_usage=reply_usage,
            reply_total=reply_total,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        allowed_sources=frozenset(CandidateSource),
        allowed_actions=frozenset(ActionKind),
        policy_approved=True,
    )


def _candidate(
    *,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    now: datetime = NOW,
    root_id: str = ROOT_ID,
    parent_id: str = PARENT_ID,
    reply_id: str = REPLY_ID,
    reply_text: str = REPLY_TEXT,
    label: str = "one",
) -> ObservedCandidate:
    observed_at = now - timedelta(minutes=1)
    binding = VerifiedTargetBinding(
        account_id=account_id,
        actor_id=actor_id,
        source=CandidateSource.INBOUND_REPLY,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        target_kind=TargetKind.INBOUND_REPLY,
        thread_id=root_id,
        reply_id=reply_id,
        root_id=root_id,
        parent_id=parent_id,
        reply_observation=RemoteReply(
            reply_id=reply_id,
            text=reply_text,
            timestamp="2026-10-13T03:59:00Z",
            root_post_id=root_id,
            replied_to_id=parent_id,
            is_reply_owned_by_me=False,
        ),
        observed_at=observed_at,
        completeness=ContextCompleteness.COMPLETE,
        context_evidence_sha256=_sha(f"placeholder-{label}"),
        actor_verified=True,
        source_verified=True,
        relationship_verified=True,
        root_owned_by_account=True,
        reply_owned_by_operator=False,
        public_context=True,
        reply_control_allows_reply=True,
    )
    candidate = ObservedCandidate(
        account_id=account_id,
        source=CandidateSource.INBOUND_REPLY,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        action=ActionKind.REPLY,
        fingerprint=_sha(f"synthetic-candidate-{label}"),
        score=10,
        source_order=0,
        state=CandidateState.ELIGIBLE,
        observed_at=observed_at,
        binding=binding,
    )
    display = _display(candidate)
    return replace(
        candidate,
        binding=replace(binding, context_evidence_sha256=review_display_context_sha256(display)),
    )


def _display(
    candidate: ObservedCandidate,
    *,
    root_id: str | None = None,
    parent_id: str | None = None,
    reply_id: str | None = None,
    root_text: str = ROOT_TEXT,
    parent_text: str = PARENT_TEXT,
    reply_text: str = REPLY_TEXT,
) -> ReviewDisplaySnapshot:
    binding = candidate.binding
    assert binding is not None
    visible_root_id = binding.root_id if root_id is None else root_id
    return ReviewDisplaySnapshot(
        account_id=candidate.account_id,
        actor_id=binding.actor_id,
        action=candidate.action,
        source=candidate.source,
        provenance=candidate.provenance,
        candidate_fingerprint=candidate.fingerprint,
        thread_id=visible_root_id,
        reply_id=binding.reply_id if reply_id is None else reply_id,
        root_id=visible_root_id,
        parent_id=binding.parent_id if parent_id is None else parent_id,
        reply_text=reply_text,
        root_text=root_text,
        parent_text=parent_text,
        observed_at=binding.observed_at,
    )


def _scenario(
    *,
    text: str = CONTENT_TEXT,
    label: str = "one",
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    now: datetime = NOW,
    candidate: ObservedCandidate | None = None,
    scope: CampaignScope | None = None,
    display: ReviewDisplaySnapshot | None = None,
) -> tuple[
    CampaignScope,
    ObservedCandidate,
    ReviewDisplaySnapshot,
    ReviewContentPacket,
    PrivateReviewPreview,
]:
    selected_scope = scope or _scope(account_id=account_id, actor_id=actor_id, now=now)
    selected_candidate = candidate or _candidate(
        account_id=account_id, actor_id=actor_id, now=now, label=label
    )
    selected_display = display or _display(selected_candidate)
    plan = plan_cycle(selected_scope, (selected_candidate,), None, now=now)
    assert plan.decision is PlanDecision.OBSERVE_ONLY
    intents = tuple(item.review_intent for item in plan.items if item.review_intent is not None)
    assert len(intents) == 1
    intent = intents[0]
    assert type(intent) is ReviewIntent and intent.dispatchable is False
    prepared = PreparedContent(
        text=text,
        origin=ContentOrigin.SYNTHETIC_FIXTURE,
        rights=RightsEvidence(
            source_attribution=ATTRIBUTION,
            rights_basis=RightsBasis.SYNTHETIC_FIXTURE,
            right_to_use_declared=True,
            job_facts_sha256=_sha("synthetic job facts"),
            job_facts_declaration=JobFactsDeclaration.SYNTHETIC_FIXTURE,
        ),
        prepared_at=now - timedelta(minutes=2),
        effective_at=now - timedelta(minutes=1),
        expires_at=min(intent.expires_at, now + timedelta(minutes=8)),
    )
    packet_result = prepare_review_packet(
        intent, prepared, selected_scope, (selected_candidate,), now=now
    )
    assert packet_result.decision is ContentDecision.NEEDS_REVIEW
    assert packet_result.reason_codes == ()
    packet = packet_result.packet
    assert type(packet) is ReviewContentPacket
    preview_result = build_private_review_preview(
        packet, selected_scope, (selected_candidate,), selected_display, now=now
    )
    assert preview_result.decision is PrivateReviewDecision.READY_FOR_PRIVATE_TTY
    assert preview_result.reason_codes == ()
    preview = preview_result.preview
    assert type(preview) is PrivateReviewPreview
    return selected_scope, selected_candidate, selected_display, packet, preview


def _store_root(tmp_path: Path) -> Path:
    root = tmp_path / "private-data"
    if os.name == "nt":
        from threads_platform.standalone.api_auto_review_stage import _set_windows_private_acl

        # These are fresh pytest fixture directories. Narrow them so the
        # private-root fixture has no inherited broad writer above it.
        _set_windows_private_acl(tmp_path.parent, directory=True)
        _set_windows_private_acl(tmp_path, directory=True)
    root.mkdir()
    if os.name == "nt":
        from threads_platform.standalone.api_auto_review_stage import _set_windows_private_acl

        _set_windows_private_acl(root, directory=True)
    else:
        root.chmod(0o700)
    return root


def _stage(
    root: Path,
    scenario: tuple[
        CampaignScope,
        ObservedCandidate,
        ReviewDisplaySnapshot,
        ReviewContentPacket,
        PrivateReviewPreview,
    ],
    *,
    now: datetime = NOW,
):
    scope, candidate, display, packet, preview = scenario
    return PrivateReviewStageStore(root).stage_review(
        packet, preview, scope, (candidate,), display, now=now
    )


def _record_path(root: Path, stage_id: UUID, account_id: UUID = ACCOUNT_ID) -> Path:
    return root / "api_auto_review_stage_v1" / str(account_id) / f"{stage_id}.json"


def test_stage_and_fresh_store_restart_preserve_only_review_material(tmp_path: Path) -> None:
    scenario = _scenario()
    root = _store_root(tmp_path)
    result = _stage(root, scenario)

    assert result.decision is ReviewStageDecision.STAGED
    assert result.reason_codes == ()
    assert type(result.stage_id) is UUID
    record_path = _record_path(root, result.stage_id)
    raw = record_path.read_bytes()
    for forbidden in (ACTOR_ID, ROOT_ID, PARENT_ID, REPLY_ID, ROOT_TEXT, PARENT_TEXT, REPLY_TEXT):
        assert forbidden.encode() not in raw
    assert CONTENT_TEXT.encode() in raw
    assert ATTRIBUTION.encode() in raw

    restarted_store = PrivateReviewStageStore(root)
    loaded = restarted_store.get_stage(ACCOUNT_ID, result.stage_id, now=NOW + timedelta(minutes=1))
    assert loaded.decision is ReviewStageDecision.LOADED
    assert loaded.reason_codes == ()
    assert type(loaded.stage) is StagedReviewV1
    assert loaded.stage.state is ReviewStageState.REVIEW_ONLY
    assert loaded.stage.dispatchable is False
    assert loaded.stage.account_id == ACCOUNT_ID
    assert loaded.stage.content_sha256 == scenario[3].content_sha256
    assert loaded.stage.packet_revision_sha256 == scenario[3].packet_revision_sha256
    assert loaded.stage.preview_sha256 == scenario[4].preview_sha256
    assert loaded.stage.root_sha256 == scenario[3].binding.root_sha256
    assert loaded.stage.parent_sha256 == scenario[3].binding.parent_sha256
    assert not hasattr(loaded.stage, "approval_id")
    assert not hasattr(loaded.stage, "approved")
    assert not hasattr(loaded.stage, "consent")

    private_text = restarted_store.load_private_text(
        ACCOUNT_ID, result.stage_id, now=NOW + timedelta(minutes=1)
    )
    assert private_text.decision is ReviewStageDecision.LOADED
    assert private_text.content is not None
    assert private_text.content.normalized_text == CONTENT_TEXT
    assert private_text.content.source_attribution == ATTRIBUTION
    assert private_text.content.content_sha256 == scenario[3].content_sha256
    assert private_text.content.dispatchable is False
    assert CONTENT_TEXT not in repr(private_text.content)
    assert ACTOR_ID not in repr(loaded)
    assert ROOT_ID not in repr(loaded.stage)
    assert str(root) not in repr(PrivateReviewStageStore(root))
    if os.name != "nt":
        assert record_path.stat().st_mode & 0o777 == 0o600
        assert record_path.parent.stat().st_mode & 0o777 == 0o700


def test_same_packet_and_preview_cannot_be_staged_twice(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    scenario = _scenario()

    first = _stage(root, scenario)
    second = _stage(root, scenario)

    assert first.decision is ReviewStageDecision.STAGED
    assert second.decision is ReviewStageDecision.STOP
    assert second.reason_codes == (ReviewStageReason.DUPLICATE_STAGE,)
    assert len(list((root / "api_auto_review_stage_v1" / str(ACCOUNT_ID)).glob("*.json"))) == 1


@pytest.mark.parametrize(
    "failure",
    ["account", "actor", "target", "root", "parent", "text", "permission", "quota", "preview"],
)
def test_changed_or_ineligible_evidence_creates_no_record(tmp_path: Path, failure: str) -> None:
    scope, candidate, display, packet, preview = _scenario()
    if failure == "account":
        scope = _scope(account_id=OTHER_ACCOUNT_ID)
    elif failure == "actor":
        scope = _scope(actor_id="synthetic-other-actor")
    elif failure == "target":
        display = replace(display, reply_id="synthetic-changed-reply")
    elif failure == "root":
        display = replace(display, root_text="Synthetic changed root text.")
    elif failure == "parent":
        display = replace(display, parent_text="Synthetic changed parent text.")
    elif failure == "text":
        packet = replace(
            packet, prepared_content=replace(packet.prepared_content, text="Changed after preview.")
        )
    elif failure == "permission":
        scope = _scope(permissions=frozenset({ApiPermission.THREADS_BASIC}))
    elif failure == "quota":
        scope = _scope(reply_usage=100, reply_total=100)
    elif failure == "preview":
        preview = replace(preview, preview_sha256=_sha("wrong preview"))

    root = _store_root(tmp_path)
    result = PrivateReviewStageStore(root).stage_review(
        packet, preview, scope, (candidate,), display, now=NOW
    )

    assert result.decision is ReviewStageDecision.STOP
    assert result.stage_id is None
    assert not list(root.rglob("*.json"))


def test_expired_context_and_wrong_account_reopen_stop(tmp_path: Path) -> None:
    scenario = _scenario()
    root = _store_root(tmp_path)
    result = _stage(root, scenario)
    assert result.stage_id is not None
    store = PrivateReviewStageStore(root)

    expired = store.get_stage(ACCOUNT_ID, result.stage_id, now=scenario[4].expires_at)
    wrong_account = store.get_stage(OTHER_ACCOUNT_ID, result.stage_id, now=NOW)

    assert expired.decision is ReviewStageDecision.STOP
    assert expired.reason_codes == (ReviewStageReason.EXPIRED,)
    assert wrong_account.decision is ReviewStageDecision.STOP
    assert wrong_account.reason_codes == (ReviewStageReason.STAGE_NOT_FOUND,)


def test_expired_preview_cannot_be_staged(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    scenario = _scenario()
    expired = _stage(root, scenario, now=scenario[4].expires_at)

    assert expired.decision is ReviewStageDecision.STOP
    assert expired.reason_codes == (ReviewStageReason.EXPIRED,)
    assert not list(root.rglob("*.json"))


def test_identical_synthetic_packet_material_is_isolated_by_account(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    first_scenario = _scenario(label="same-content", text="Same synthetic text.")
    second_candidate = _candidate(
        account_id=OTHER_ACCOUNT_ID,
        actor_id="synthetic-actor-18",
        label="same-content",
    )
    second_scenario = _scenario(
        label="same-content",
        text="Same synthetic text.",
        account_id=OTHER_ACCOUNT_ID,
        actor_id="synthetic-actor-18",
        candidate=second_candidate,
    )

    first = _stage(root, first_scenario)
    second = _stage(root, second_scenario)

    assert first.decision is ReviewStageDecision.STAGED
    assert second.decision is ReviewStageDecision.STAGED
    assert first.stage_id != second.stage_id
    assert (
        _record_path(root, first.stage_id).parent
        != _record_path(root, second.stage_id, OTHER_ACCOUNT_ID).parent
    )
    wrong_account = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, second.stage_id, now=NOW)
    assert wrong_account.decision is ReviewStageDecision.STOP


@pytest.mark.parametrize("count", [0, 1, _MAX_STAGES_PER_ACCOUNT, _MAX_STAGES_PER_ACCOUNT + 1])
def test_queue_limit_boundaries_are_64_and_fail_closed(tmp_path: Path, count: int) -> None:
    root = _store_root(tmp_path)
    store = PrivateReviewStageStore(root)
    scenario = _scenario(text="Synthetic queue boundary packet.")
    if count == 0:
        outcomes = []
    elif count == 1:
        outcomes = [_stage(root, scenario)]
    else:
        _namespace, account_dir = store._prepare_account(ACCOUNT_ID)
        base = stage_module._make_staged_review(
            scenario[3], scenario[4], scenario[0], UUID(int=1000), NOW
        )
        for index in range(count - 1):
            seeded = replace(
                base,
                stage_id=UUID(int=index + 1),
                packet_revision_sha256=_sha(f"seed-packet-{index}"),
                preview_sha256=_sha(f"seed-preview-{index}"),
            )
            encoded = stage_module._encode_record(
                seeded, scenario[3].prepared_content.text, ATTRIBUTION
            )
            stage_module._atomic_create_record(account_dir, seeded, encoded)
        extra = _scenario(text=f"Synthetic queue packet {count:03d}.", label=f"extra-{count}")
        outcomes = [_stage(root, extra)]

    assert len(list(root.rglob("*.json"))) == min(count, _MAX_STAGES_PER_ACCOUNT)
    if count in {1, _MAX_STAGES_PER_ACCOUNT}:
        assert outcomes[-1].decision is ReviewStageDecision.STAGED
    if count == _MAX_STAGES_PER_ACCOUNT + 1:
        assert outcomes[-1].decision is ReviewStageDecision.STOP
        assert outcomes[-1].reason_codes == (ReviewStageReason.QUEUE_FULL,)


@pytest.mark.parametrize(
    "contents",
    [
        '{"schema":"api_auto_review_stage_v1","schema":"api_auto_review_stage_v1"}',
        "{",
    ],
)
def test_duplicate_keys_and_partial_json_fail_closed(tmp_path: Path, contents: str) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    path = _record_path(root, result.stage_id)
    path.write_text(contents, encoding="utf-8")

    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.CORRUPT_RECORD,)


def test_non_finite_json_number_fails_closed(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    path = _record_path(root, result.stage_id)
    raw = path.read_text(encoding="utf-8")
    assert '"quota_total":100' in raw
    path.write_text(raw.replace('"quota_total":100', '"quota_total":NaN'), encoding="utf-8")

    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.CORRUPT_RECORD,)


@pytest.mark.parametrize("tamper", ["extra", "version", "state", "content", "account"])
def test_schema_drift_and_digest_tampering_fail_closed(tmp_path: Path, tamper: str) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    path = _record_path(root, result.stage_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "extra":
        record["unexpected"] = "value"
    elif tamper == "version":
        record["schema_version"] = 2
    elif tamper == "state":
        record["state"] = "APPROVED"
    elif tamper == "content":
        record["private_content"]["normalized_text"] = "Changed on disk."
    elif tamper == "account":
        record["account_id"] = str(OTHER_ACCOUNT_ID)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.CORRUPT_RECORD,)


def test_restart_preserves_incomplete_private_temp_and_stops(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    account_dir = root / "api_auto_review_stage_v1" / str(ACCOUNT_ID)
    orphan = account_dir / f".stage-{UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')}.tmp"
    partial_record = b'{"partial":'
    orphan.write_bytes(partial_record)
    if os.name != "nt":
        orphan.chmod(0o600)

    reopened = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert reopened.decision is ReviewStageDecision.STOP
    assert reopened.reason_codes == (ReviewStageReason.INCOMPLETE_TEMPORARY_RECORD,)
    assert orphan.read_bytes() == partial_record


def test_held_account_lock_stops_store_and_another_process(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    scenario = _scenario()
    first = _stage(root, scenario)
    assert first.decision is ReviewStageDecision.STAGED
    lock_path = root / "api_auto_review_stage_v1" / str(ACCOUNT_ID) / ".lock"
    held = FilesystemProcessLock(lock_path)
    held.acquire()
    try:
        script = """
import sys
from pathlib import Path
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
lock = FilesystemProcessLock(Path(sys.argv[1]))
try:
    lock.acquire()
except ProcessAlreadyRunning:
    print("LOCK_BUSY")
else:
    print("ACQUIRED")
    lock.release()
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        contender_scenario = _scenario(text="A different synthetic packet.", label="lock-contender")
        contender = _stage(root, contender_scenario)
    finally:
        held.release()

    assert child.returncode == 0
    assert child.stdout.strip() == "LOCK_BUSY"
    assert contender.decision is ReviewStageDecision.STOP
    assert contender.reason_codes == (ReviewStageReason.LOCK_BUSY,)


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ("before_temp_write", ReviewStageReason.IO_FAILURE),
        ("after_temp_write_before_fsync", ReviewStageReason.IO_FAILURE),
        ("after_file_fsync", ReviewStageReason.IO_FAILURE),
        ("before_atomic_publish", ReviewStageReason.IO_FAILURE),
        ("after_atomic_publish", ReviewStageReason.PUBLICATION_AMBIGUOUS),
    ],
)
def test_crash_boundaries_never_create_approval_and_restart_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, expected: ReviewStageReason
) -> None:
    root = _store_root(tmp_path)
    scenario = _scenario()
    generated_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    monkeypatch.setattr(stage_module, "uuid4", lambda: generated_id)

    def fail_at(name: str) -> None:
        if name == point:
            raise OSError("synthetic crash boundary")

    monkeypatch.setattr(stage_module, "_stage_failpoint", fail_at)
    result = _stage(root, scenario)

    assert result.decision is ReviewStageDecision.STOP
    assert result.reason_codes == (expected,)
    record_path = _record_path(root, generated_id)
    if point == "after_atomic_publish":
        assert record_path.is_file()
        monkeypatch.setattr(stage_module, "_stage_failpoint", lambda _name: None)
        reopened = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, generated_id, now=NOW)
        assert reopened.decision is ReviewStageDecision.LOADED
        assert reopened.stage is not None
        assert reopened.stage.state is ReviewStageState.REVIEW_ONLY
    else:
        assert not record_path.exists()
        temporary_path = record_path.with_name(f".stage-{generated_id}.tmp")
        assert temporary_path.is_file()
        monkeypatch.setattr(stage_module, "_stage_failpoint", lambda _name: None)
        reopened = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, generated_id, now=NOW)
        assert reopened.decision is ReviewStageDecision.STOP
        assert reopened.reason_codes == (ReviewStageReason.INCOMPLETE_TEMPORARY_RECORD,)


def test_hard_link_alias_is_rejected(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    record_path = _record_path(root, result.stage_id)
    alias = record_path.with_name("alias.json")
    os.link(record_path, alias)

    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.UNSAFE_PATH,)


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are platform-specific")
def test_junctioned_account_directory_is_rejected(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    staged = _stage(root, _scenario())
    assert staged.stage_id is not None
    namespace = root / "api_auto_review_stage_v1"
    junction = namespace / str(OTHER_ACCOUNT_ID)
    command_processor = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    command = f'mklink /J "{junction}" "{root}"'
    created = subprocess.run(
        [str(command_processor), "/d", "/c", command],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("the synthetic Windows filesystem did not permit a junction fixture")

    loaded = PrivateReviewStageStore(root).get_stage(
        OTHER_ACCOUNT_ID, UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), now=NOW
    )

    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.UNSAFE_PATH,)


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires elevated Windows privileges"
)
def test_symlinked_account_record_is_rejected(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    result = _stage(root, _scenario())
    assert result.stage_id is not None
    record_path = _record_path(root, result.stage_id)
    copy_path = record_path.with_name("copy.json")
    copy_path.write_bytes(record_path.read_bytes())
    if os.name != "nt":
        copy_path.chmod(0o600)
    record_path.unlink()
    record_path.symlink_to(copy_path)

    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, result.stage_id, now=NOW)

    assert loaded.decision is ReviewStageDecision.STOP


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_group_or_other_readable_root_and_record_are_rejected(tmp_path: Path) -> None:
    root = _store_root(tmp_path)
    scenario = _scenario()
    root.chmod(0o755)
    rejected_root = _stage(root, scenario)
    assert rejected_root.decision is ReviewStageDecision.STOP
    assert rejected_root.reason_codes == (ReviewStageReason.UNSAFE_DATA_ROOT,)

    root.chmod(0o700)
    accepted = _stage(root, scenario)
    assert accepted.stage_id is not None
    record_path = _record_path(root, accepted.stage_id)
    record_path.chmod(0o644)
    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, accepted.stage_id, now=NOW)
    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.ACL_UNSAFE,)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL verification is platform-specific")
def test_broadened_windows_root_and_record_acls_are_rejected(tmp_path: Path) -> None:
    from threads_platform.standalone.api_auto_review_stage import _set_windows_private_acl

    root = _store_root(tmp_path)
    _set_windows_private_acl(root, directory=True)
    system_root = os.environ["SystemRoot"]
    icacls = Path(system_root) / "System32" / "icacls.exe"

    broaden_root = subprocess.run(
        [str(icacls), str(root), "/grant", "*S-1-1-0:(OI)(CI)R", "/q"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert broaden_root.returncode == 0
    blocked_root = _stage(root, _scenario())
    assert blocked_root.decision is ReviewStageDecision.STOP
    assert blocked_root.reason_codes == (ReviewStageReason.UNSAFE_DATA_ROOT,)

    _set_windows_private_acl(root, directory=True)
    accepted = _stage(root, _scenario())
    assert accepted.stage_id is not None
    record_path = _record_path(root, accepted.stage_id)
    broaden_record = subprocess.run(
        [str(icacls), str(record_path), "/grant", "*S-1-1-0:R", "/q"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert broaden_record.returncode == 0
    loaded = PrivateReviewStageStore(root).get_stage(ACCOUNT_ID, accepted.stage_id, now=NOW)
    assert loaded.decision is ReviewStageDecision.STOP
    assert loaded.reason_codes == (ReviewStageReason.ACL_UNSAFE,)


def test_repository_checkout_cannot_be_used_as_data_root() -> None:
    checkout = Path(__file__).resolve().parents[1]
    scope, candidate, display, packet, preview = _scenario()
    result = PrivateReviewStageStore(checkout).stage_review(
        packet,
        preview,
        scope,
        (candidate,),
        display,
        now=NOW,
    )
    assert result.decision is ReviewStageDecision.STOP
    assert result.reason_codes == (ReviewStageReason.UNSAFE_DATA_ROOT,)


def test_foreign_receipt_or_approval_shape_is_not_an_input() -> None:
    signature = PrivateReviewStageStore.stage_review
    parameter_names = tuple(signature.__annotations__)
    assert "receipt" not in parameter_names
    assert "approval" not in parameter_names
    assert "dispatch" not in parameter_names


def test_stage_module_imports_no_api_browser_or_credential_runtime() -> None:
    source = inspect.getsource(stage_module)
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    forbidden_roots = {"httpx", "httpx2", "requests", "playwright", "keyring"}
    assert not any(name.split(".", maxsplit=1)[0] in forbidden_roots for name in imported)
    assert "threads_platform.standalone.api_auto_review_tty" not in imported
