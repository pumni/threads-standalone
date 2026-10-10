from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.mutations import LocalOperation, LocalOperationStore
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_content import (
    content_fingerprint,
    draft_fingerprint,
    source_fingerprint,
)
from threads_platform.standalone.nurture_insights import insights_snapshot_id
from threads_platform.standalone.nurture_store import (
    NurtureContentRecordV1,
    NurtureInsightsSnapshotV1,
    NurtureStore,
    NurtureTargetV1,
    fingerprint_remote_thread,
)
from threads_platform.standalone.recurrences import LocalRecurrenceStore, RecurrenceRecord

_NOW = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
_CREDENTIAL_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC_REHEARSAL"
_WORKFLOW_BYTES = b'{"version":1,"account":"RehearsalCase","steps":[{"action":"quota"}]}\n'


@dataclass(frozen=True, slots=True)
class _SyntheticState:
    root: Path
    account: LocalAccount
    preset: NurturePresetV1
    operations: dict[str, LocalOperation]
    target_states: dict[str, NurtureTargetV1]
    content_records: dict[str, NurtureContentRecordV1]
    completed_run_id: UUID
    active_run_id: UUID
    recurrence: RecurrenceRecord
    workflow_bytes: bytes
    insight: NurtureInsightsSnapshotV1


def _publish_operation(
    store: LocalOperationStore,
    account_id: UUID,
    *,
    kind: str,
    result_phase: str,
    role: str,
) -> LocalOperation:
    operation = store.create_received(account_id, kind=kind)
    if result_phase == "FAILED_FINAL":
        return store.update(
            replace(operation, phase="FAILED_FINAL", outcome_code="SYNTHETIC_REJECTION")
        )

    if kind == "POST_CAROUSEL":
        operation = store.update(replace(operation, phase="CHILDREN_CREATING"))
        operation = store.update(
            replace(
                operation,
                child_container_ids=("synthetic-carousel-child-one",),
            )
        )
        operation = store.update(
            replace(
                operation,
                child_container_ids=(
                    *operation.child_container_ids,
                    "synthetic-carousel-child-two",
                ),
            )
        )

    operation = store.update(
        replace(
            operation,
            phase="CONTAINER_CREATED",
            container_id=f"synthetic-container-{role}",
        )
    )
    operation = store.update(replace(operation, phase="PUBLISH_REQUESTED"))
    if result_phase == "PUBLISH_REQUESTED":
        return operation
    if result_phase == "AMBIGUOUS":
        return store.update(replace(operation, phase="AMBIGUOUS", outcome_code="SYNTHETIC_UNKNOWN"))
    if result_phase == "PUBLISHED":
        return store.update(
            replace(operation, phase="PUBLISHED", media_id=f"synthetic-media-{role}")
        )
    raise AssertionError("unsupported synthetic journal phase")


def _moderation_operation(
    store: LocalOperationStore,
    account_id: UUID,
    *,
    result_phase: str,
) -> LocalOperation:
    operation = store.create_received(
        account_id,
        kind="MODERATE_REPLY",
        action="approve",
    )
    operation = store.update(replace(operation, phase="MUTATION_REQUESTED"))
    if result_phase == "MUTATION_REQUESTED":
        return operation
    if result_phase == "CONFIRMED":
        return store.update(replace(operation, phase="CONFIRMED"))
    if result_phase == "AMBIGUOUS":
        return store.update(replace(operation, phase="AMBIGUOUS", outcome_code="SYNTHETIC_UNKNOWN"))
    raise AssertionError("unsupported synthetic moderation phase")


def _create_synthetic_state(tmp_path: Path) -> _SyntheticState:
    root = tmp_path / "synthetic-data-root"
    account_store = LocalAccountStore(root)
    account = account_store.add("RehearsalCase")
    account = account_store.set_credential_ref(account.alias, _CREDENTIAL_REF)

    account_lock = FilesystemProcessLock(root / "locks" / f"{account.id}.lock")
    account_lock.acquire()
    account_lock.release()

    operations_store = LocalOperationStore(root)
    operations = {
        "publish_requested": _publish_operation(
            operations_store,
            account.id,
            kind="POST_TEXT",
            result_phase="PUBLISH_REQUESTED",
            role="in-flight-text",
        ),
        "quote_published": _publish_operation(
            operations_store,
            account.id,
            kind="POST_QUOTE",
            result_phase="PUBLISHED",
            role="quote",
        ),
        "reply_ambiguous": _publish_operation(
            operations_store,
            account.id,
            kind="CREATE_REPLY",
            result_phase="AMBIGUOUS",
            role="ambiguous-reply",
        ),
        "image_failed": _publish_operation(
            operations_store,
            account.id,
            kind="POST_IMAGE",
            result_phase="FAILED_FINAL",
            role="image-failure",
        ),
        "video_published": _publish_operation(
            operations_store,
            account.id,
            kind="POST_VIDEO",
            result_phase="PUBLISHED",
            role="video",
        ),
        "carousel_published": _publish_operation(
            operations_store,
            account.id,
            kind="POST_CAROUSEL",
            result_phase="PUBLISHED",
            role="carousel",
        ),
        "moderation_requested": _moderation_operation(
            operations_store,
            account.id,
            result_phase="MUTATION_REQUESTED",
        ),
        "moderation_confirmed": _moderation_operation(
            operations_store,
            account.id,
            result_phase="CONFIRMED",
        ),
        "reply_confirmed": _publish_operation(
            operations_store,
            account.id,
            kind="CREATE_REPLY",
            result_phase="PUBLISHED",
            role="confirmed-reply",
        ),
        "content_published": _publish_operation(
            operations_store,
            account.id,
            kind="POST_TEXT",
            result_phase="PUBLISHED",
            role="content-published",
        ),
        "content_ambiguous": _publish_operation(
            operations_store,
            account.id,
            kind="POST_TEXT",
            result_phase="AMBIGUOUS",
            role="content-ambiguous",
        ),
    }

    preset = get_nurture_preset("recruitment")
    nurture_store = NurtureStore(root)
    target_states: dict[str, NurtureTargetV1] = {}
    content_records: dict[str, NurtureContentRecordV1] = {}
    with nurture_store.acquire_account_lock(account.id) as owner:
        completed_scope = owner.start_run(preset, now=_NOW)
        run_id = completed_scope.receipt.id
        target_inputs = (
            ("confirmed", "CONFIRMED", operations["reply_confirmed"]),
            ("pending", "PENDING", None),
            ("ambiguous", "AMBIGUOUS", operations["reply_ambiguous"]),
            ("observed", "NONE", None),
        )
        for name, state, operation in target_inputs:
            fingerprint = fingerprint_remote_thread(f"synthetic-target-{name}")
            target = owner.observe_target(
                preset,
                fingerprint,
                run_id,
                "SYNTHETIC_REHEARSAL",
                now=_NOW,
            )
            if state != "NONE":
                target = owner.reserve_target(
                    preset,
                    fingerprint,
                    run_id,
                    "SYNTHETIC_REHEARSAL",
                    now=_NOW,
                )
                if state in {"CONFIRMED", "AMBIGUOUS"}:
                    assert operation is not None
                    target = owner.complete_target_action(
                        preset,
                        fingerprint,
                        run_id,
                        state,
                        operation.id,
                        now=_NOW,
                    )
            target_states[name] = target

        content_inputs = (
            ("published", "PUBLISHED", operations["content_published"]),
            ("reserved", "RESERVED", None),
            ("ambiguous", "AMBIGUOUS", operations["content_ambiguous"]),
        )
        for name, state, operation in content_inputs:
            raw_source = f"synthetic-content-source-{name}"
            raw_draft = f"Synthetic content draft {name}"
            source_fp = source_fingerprint("OPERATOR_SOURCE_ID", raw_source)
            draft_fp = draft_fingerprint(raw_draft)
            record = owner.register_content_candidate(
                preset,
                uuid4(),
                source_fp,
                draft_fp,
                "CAREER_TIP",
                run_id,
                now=_NOW,
            )
            if state != "NEW":
                record = owner.reserve_content_candidate(preset, source_fp, draft_fp, run_id)
                if state in {"PUBLISHED", "AMBIGUOUS"}:
                    assert operation is not None
                    record = owner.complete_content_candidate(
                        preset,
                        source_fp,
                        draft_fp,
                        run_id,
                        state,
                        operation.id,
                        now=_NOW,
                    )
            content_records[name] = record

        published = content_records["published"]
        content_fp = content_fingerprint(
            published.source_fingerprint,
            published.draft_fingerprint,
        )
        insight = NurtureInsightsSnapshotV1(
            version=1,
            snapshot_id=insights_snapshot_id(content_fp, _NOW),
            account_id=account.id,
            preset_id=preset.id,
            content_fingerprint=content_fp,
            source_fingerprint=published.source_fingerprint,
            draft_fingerprint=published.draft_fingerprint,
            category=published.category,
            observed_at=_NOW,
            likes=None,
            replies=2,
            reposts=None,
            quotes=0,
            operation_id=operations["content_published"].id,
        )
        owner.append_insights_snapshot(preset, insight)
        for role in (
            "reply_confirmed",
            "reply_ambiguous",
            "content_published",
            "content_ambiguous",
        ):
            completed_scope.link_operation(operations[role].id)
        completed_scope.add_decision("SYNTHETIC_REHEARSAL")
        completed = completed_scope.finish("SUCCESS", now=_NOW + timedelta(seconds=1))

        active_scope = owner.start_run(preset, now=_NOW + timedelta(seconds=2))
        active_run_id = active_scope.receipt.id

    workflow_path = tmp_path / "synthetic-inputs" / "workflow.json"
    workflow_path.parent.mkdir()
    workflow_path.write_bytes(_WORKFLOW_BYTES)
    recurrence_store = LocalRecurrenceStore(root, account_store)
    recurrence = recurrence_store.create(workflow_path, 3600, now=_NOW)
    recurrence = recurrence_store.disable(recurrence.id)

    assert {operation.kind for operation in operations.values()} == {
        "POST_TEXT",
        "POST_QUOTE",
        "CREATE_REPLY",
        "POST_IMAGE",
        "POST_VIDEO",
        "POST_CAROUSEL",
        "MODERATE_REPLY",
    }
    return _SyntheticState(
        root=root,
        account=account,
        preset=preset,
        operations=operations,
        target_states=target_states,
        content_records=content_records,
        completed_run_id=completed.id,
        active_run_id=active_run_id,
        recurrence=recurrence,
        workflow_bytes=_WORKFLOW_BYTES,
        insight=insight,
    )


def _manifest(state: _SyntheticState) -> dict[str, Any]:
    return {
        "account": {
            "id": str(state.account.id),
            "alias": state.account.alias,
            "credential_ref": state.account.credential_ref,
        },
        "preset_id": state.preset.id,
        "operations": [
            {
                "name": name,
                "id": str(operation.id),
                "account_id": str(operation.account_id),
                "kind": operation.kind,
                "phase": operation.phase,
                "container_id": operation.container_id,
                "media_id": operation.media_id,
                "outcome_code": operation.outcome_code,
                "child_container_ids": list(operation.child_container_ids),
                "action": operation.action,
            }
            for name, operation in state.operations.items()
        ],
        "completed_run_id": str(state.completed_run_id),
        "active_run_id": str(state.active_run_id),
        "receipt_operation_ids": sorted(
            str(state.operations[name].id)
            for name in (
                "reply_confirmed",
                "reply_ambiguous",
                "content_published",
                "content_ambiguous",
            )
        ),
        "targets": {
            name: {
                "fingerprint": target.fingerprint,
                "action_state": target.action_state,
                "last_run_id": str(target.last_run_id),
                "last_operation_id": (
                    None if target.last_operation_id is None else str(target.last_operation_id)
                ),
            }
            for name, target in state.target_states.items()
        },
        "content": {
            name: {
                "candidate_id": str(record.candidate_id),
                "source_fingerprint": record.source_fingerprint,
                "draft_fingerprint": record.draft_fingerprint,
                "publication_state": record.publication_state,
                "last_run_id": str(record.last_run_id),
                "operation_id": None if record.operation_id is None else str(record.operation_id),
            }
            for name, record in state.content_records.items()
        },
        "insight": {
            "snapshot_id": state.insight.snapshot_id,
            "content_fingerprint": state.insight.content_fingerprint,
            "source_fingerprint": state.insight.source_fingerprint,
            "draft_fingerprint": state.insight.draft_fingerprint,
            "operation_id": str(state.insight.operation_id),
            "metrics": [
                state.insight.likes,
                state.insight.replies,
                state.insight.reposts,
                state.insight.quotes,
            ],
        },
        "recurrence": {
            "id": state.recurrence.id,
            "account": state.recurrence.account,
            "status": state.recurrence.status,
            "interval_seconds": state.recurrence.interval_seconds,
            "anchor_at": state.recurrence.anchor_at.isoformat(),
            "next_due_at": state.recurrence.next_due_at.isoformat(),
        },
        "workflow_hex": state.workflow_bytes.hex(),
        "now": _NOW.isoformat(),
    }


def _inventory(root: Path) -> dict[str, tuple[bytes, str]]:
    if not root.is_dir():
        pytest.fail("synthetic state root is missing")
    result: dict[str, tuple[bytes, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            contents = path.read_bytes()
            result[path.relative_to(root).as_posix()] = (
                contents,
                hashlib.sha256(contents).hexdigest(),
            )
    return result


def _read_only_child(root: Path, manifest: dict[str, Any], cwd: Path) -> dict[str, Any]:
    environment = {
        "THREADS_LOCAL_DATA_ROOT": str(root),
        "PYTHONUTF8": "1",
    }
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value

    result = subprocess.run(
        [sys.executable, "-I", "-c", _READ_ONLY_CHILD, str(root)],
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
        cwd=cwd,
        env=environment,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        try:
            failure: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError:
            failure = {}
        stage = failure.get("failed_stage", "unknown")
        if type(stage) is not str or not stage.isascii():
            stage = "unknown"
        pytest.fail(f"fresh synthetic state reader failed during {stage}")
    try:
        summary: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("fresh synthetic state reader returned no bounded summary")
    if summary.get("passed") is not True:
        pytest.fail("fresh synthetic state reader did not confirm all compatibility checks")
    if result.stderr:
        pytest.fail("fresh synthetic state reader wrote unexpected diagnostics")

    private_values = [
        manifest["account"]["id"],
        manifest["account"]["alias"],
        manifest["account"]["credential_ref"],
        *[item["id"] for item in manifest["operations"]],
        *[item["fingerprint"] for item in manifest["targets"].values()],
        *[item["source_fingerprint"] for item in manifest["content"].values()],
        *[item["draft_fingerprint"] for item in manifest["content"].values()],
    ]
    if any(value and value in result.stdout for value in private_values):
        pytest.fail("fresh synthetic state reader exposed an identifier or credential reference")
    return summary


def _assert_v1_keysets(state: _SyntheticState) -> None:
    root = state.root
    account_doc = json.loads(
        (root / "accounts" / f"{state.account.alias.casefold()}.json").read_text(encoding="utf-8")
    )
    assert set(account_doc) == {"version", "id", "alias", "credential_ref"}

    operation_paths = tuple((root / "operations").glob("*.json"))
    assert len(operation_paths) == len(state.operations)
    required_journal_fields = {"version", "id", "account_id", "kind", "phase"}
    allowed_journal_fields = required_journal_fields | {
        "container_id",
        "media_id",
        "outcome_code",
        "child_container_ids",
        "action",
    }
    for path in operation_paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert required_journal_fields <= set(document) <= allowed_journal_fields

    nurture = root / "nurture"
    completed_path = (
        nurture / "runs" / str(state.account.id) / "completed" / f"{state.completed_run_id}.json"
    )
    active_path = (
        nurture / "runs" / str(state.account.id) / "active" / f"{state.active_run_id}.json"
    )
    expected_run_fields = {
        "version",
        "id",
        "account_id",
        "preset_id",
        "preset_version",
        "started_at",
        "finished_at",
        "outcome",
        "discovered_count",
        "deduped_count",
        "selected_count",
        "enriched_count",
        "replied_count",
        "published_count",
        "skipped_count",
        "decision_codes",
        "operation_ids",
        "error_code",
        "failed_stage",
    }
    for path in (completed_path, active_path):
        assert set(json.loads(path.read_text(encoding="utf-8"))) == expected_run_fields

    target_doc = json.loads(
        (nurture / "state" / str(state.account.id) / f"{state.preset.id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(target_doc) == {"version", "account_id", "preset_id", "targets"}
    target_fields = {
        "fingerprint",
        "first_seen_at",
        "last_seen_at",
        "last_decision_code",
        "last_action_at",
        "action_state",
        "last_run_id",
        "last_operation_id",
    }
    assert all(set(target) == target_fields for target in target_doc["targets"])

    content_doc = json.loads(
        (nurture / "content" / str(state.account.id) / f"{state.preset.id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(content_doc) == {"version", "account_id", "preset_id", "candidates"}
    content_fields = {
        "candidate_id",
        "source_fingerprint",
        "draft_fingerprint",
        "category",
        "first_seen_at",
        "last_action_at",
        "publication_state",
        "last_run_id",
        "operation_id",
    }
    assert all(set(record) == content_fields for record in content_doc["candidates"])

    insight = state.insight
    insight_path = (
        nurture
        / "insights"
        / str(state.account.id)
        / state.preset.id
        / insight.content_fingerprint
        / f"{insight.snapshot_id}.json"
    )
    assert set(json.loads(insight_path.read_text(encoding="utf-8"))) == {
        "version",
        "snapshot_id",
        "account_id",
        "preset_id",
        "content_fingerprint",
        "source_fingerprint",
        "draft_fingerprint",
        "category",
        "observed_at",
        "likes",
        "replies",
        "reposts",
        "quotes",
        "operation_id",
    }

    recurrence_dir = root / "recurrences" / state.recurrence.id
    recurrence_doc = json.loads((recurrence_dir / "record.json").read_text(encoding="utf-8"))
    assert set(recurrence_doc) == {
        "version",
        "id",
        "workflow_file",
        "account",
        "interval_seconds",
        "status",
        "created_at",
        "updated_at",
        "anchor_at",
        "next_due_at",
        "last_started_at",
        "last_finished_at",
        "last_outcome",
        "last_error_code",
        "last_step_index",
    }
    assert set(json.loads((recurrence_dir / "workflow.json").read_text(encoding="utf-8"))) == {
        "version",
        "account",
        "steps",
    }


def test_synthetic_v1_codecs_and_links_match_the_documented_contract(tmp_path: Path) -> None:
    state = _create_synthetic_state(tmp_path)
    _assert_v1_keysets(state)

    assert state.account.id.version == 4
    assert state.account.alias == "RehearsalCase"
    assert state.account.credential_ref == _CREDENTIAL_REF
    assert {state.target_states[name].action_state for name in state.target_states} == {
        "CONFIRMED",
        "PENDING",
        "AMBIGUOUS",
        "NONE",
    }
    assert {record.publication_state for record in state.content_records.values()} == {
        "PUBLISHED",
        "RESERVED",
        "AMBIGUOUS",
    }
    assert all(
        target.last_run_id == state.completed_run_id for target in state.target_states.values()
    )
    assert all(
        record.last_run_id == state.completed_run_id for record in state.content_records.values()
    )
    assert state.insight.likes is None and state.insight.reposts is None
    assert state.insight.replies == 2 and state.insight.quotes == 0
    assert state.recurrence.status == "DISABLED"
    assert state.recurrence.anchor_at == _NOW
    assert (state.root / "recurrences" / state.recurrence.id / "workflow.json").read_bytes() == (
        state.workflow_bytes
    )

    serialized_state = b"\n".join(contents for contents, _ in _inventory(state.root).values())
    assert b"synthetic-content-source-" not in serialized_state
    assert b"Synthetic content draft" not in serialized_state


def test_fresh_python_reads_cold_copy_without_writes_locks_or_runtimes(tmp_path: Path) -> None:
    state = _create_synthetic_state(tmp_path)
    _assert_v1_keysets(state)

    cold_copy = tmp_path / "synthetic-cold-copy"
    shutil.copytree(state.root, cold_copy)
    original_snapshot = _inventory(state.root)
    cold_before = _inventory(cold_copy)
    if cold_before != original_snapshot:
        pytest.fail("synthetic cold copy differs before its read-only check")

    summary = _read_only_child(cold_copy, _manifest(state), tmp_path)
    required_true = {
        "account_match",
        "operations_match",
        "all_journal_kinds",
        "required_journal_phases",
        "carousel_children_linked",
        "completed_receipt_links_match",
        "active_receipt_stays_running",
        "target_states_and_links_match",
        "content_states_and_links_match",
        "insights_fingerprint_and_operation_link_match",
        "recurrence_snapshot_matches",
        "confirmed_target_within_cooldown_not_selected",
        "pending_and_ambiguous_targets_suppressed",
        "published_content_within_interval_not_due",
        "repeat_inspection_stable",
    }
    if summary.get("passed") is not True or any(
        summary.get(key) is not True for key in required_true
    ):
        pytest.fail("fresh process did not confirm all synthetic compatibility assertions")
    if _inventory(cold_copy) != cold_before:
        pytest.fail("synthetic file bytes or SHA-256 values changed during read-only inspection")
    if _inventory(state.root) != original_snapshot:
        pytest.fail("source synthetic root changed while its cold copy was inspected")


def test_failed_cold_cutover_rolls_back_to_untouched_synthetic_original(tmp_path: Path) -> None:
    state = _create_synthetic_state(tmp_path)
    original_snapshot = _inventory(state.root)
    backup = tmp_path / "synthetic-backup"
    cold_copy = tmp_path / "synthetic-cutover-copy"
    restored = tmp_path / "synthetic-rollback-root"
    shutil.copytree(state.root, backup)
    shutil.copytree(state.root, cold_copy)
    if _inventory(backup) != original_snapshot or _inventory(cold_copy) != original_snapshot:
        pytest.fail("synthetic backup or cold copy did not preserve the original bytes")

    _read_only_child(cold_copy, _manifest(state), tmp_path)
    operation = state.operations["reply_ambiguous"]
    copied_journal = cold_copy / "operations" / f"{operation.id}.json"
    copied_doc = json.loads(copied_journal.read_text(encoding="utf-8"))
    copied_doc["phase"] = "PUBLISHED"
    copied_journal.write_text(json.dumps(copied_doc, sort_keys=True), encoding="utf-8")

    # Model a failed acceptance gate by choosing the untouched backup, never merging journals.
    shutil.copytree(backup, restored)
    if _inventory(restored) != original_snapshot or _inventory(state.root) != original_snapshot:
        pytest.fail("synthetic rollback did not restore the untouched original file set")
    restored_operation = LocalOperationStore(restored).get(operation.id)
    if restored_operation.phase != "AMBIGUOUS":
        pytest.fail("synthetic rollback replaced the ambiguous journal with copy state")
    if (
        _inventory(restored)[f"operations/{operation.id}.json"]
        != original_snapshot[f"operations/{operation.id}.json"]
    ):
        pytest.fail("synthetic ambiguous journal bytes changed during rollback")


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("account_duplicate", "ACCOUNT_STATE_INVALID"),
        ("account_oversized", "ACCOUNT_STATE_INVALID"),
        ("journal_duplicate", "OPERATION_STATE_INVALID"),
        ("journal_unknown_key", "OPERATION_STATE_INVALID"),
        ("target_duplicate", "NURTURE_STATE_INVALID"),
        ("target_bad_fingerprint", "NURTURE_STATE_INVALID"),
        ("target_bad_uuid", "NURTURE_STATE_INVALID"),
        ("target_bad_timestamp", "NURTURE_STATE_INVALID"),
        ("target_unknown_key", "NURTURE_STATE_INVALID"),
        ("content_bad_fingerprint", "NURTURE_STATE_INVALID"),
        ("insight_bad_metric", "NURTURE_STATE_INVALID"),
        ("recurrence_duplicate", "RECURRENCE_STATE_INVALID"),
        ("recurrence_bad_timestamp", "RECURRENCE_STATE_INVALID"),
        ("workflow_mutation", "RECURRENCE_WORKFLOW_NOT_READ_ONLY"),
    ],
)
def test_corrupt_synthetic_v1_documents_fail_closed_without_rewrite(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    state = _create_synthetic_state(tmp_path)
    account_path = state.root / "accounts" / f"{state.account.alias.casefold()}.json"
    target_path = (
        state.root / "nurture" / "state" / str(state.account.id) / f"{state.preset.id}.json"
    )
    content_path = (
        state.root / "nurture" / "content" / str(state.account.id) / f"{state.preset.id}.json"
    )
    recurrence_dir = state.root / "recurrences" / state.recurrence.id
    recurrence_path = recurrence_dir / "record.json"
    if case == "account_duplicate":
        account_path.write_text(
            '{"version":1,"version":1,"id":"synthetic","alias":"RehearsalCase"}',
            encoding="utf-8",
        )
        corrupted_path = account_path
        read = lambda: LocalAccountStore(state.root).get(state.account.alias)
    elif case == "account_oversized":
        account_path.write_bytes(b"x" * 8193)
        corrupted_path = account_path
        read = lambda: LocalAccountStore(state.root).get(state.account.alias)
    elif case in {"journal_duplicate", "journal_unknown_key"}:
        operation = state.operations["publish_requested"]
        corrupted_path = state.root / "operations" / f"{operation.id}.json"
        if case == "journal_duplicate":
            corrupted_path.write_text('{"version":1,"version":1}', encoding="utf-8")
        else:
            document = json.loads(corrupted_path.read_text(encoding="utf-8"))
            document["unexpected"] = "synthetic"
            corrupted_path.write_text(json.dumps(document), encoding="utf-8")
        read = lambda: LocalOperationStore(state.root).get(operation.id)
    elif case.startswith("target_"):
        document = json.loads(target_path.read_text(encoding="utf-8"))
        if case == "target_duplicate":
            target_path.write_text('{"version":1,"version":1}', encoding="utf-8")
        else:
            target = document["targets"][0]
            if case == "target_bad_fingerprint":
                target["fingerprint"] = "synthetic-invalid-fingerprint"
            elif case == "target_bad_uuid":
                target["last_run_id"] = "not-a-uuid"
            elif case == "target_bad_timestamp":
                target["first_seen_at"] = "synthetic-not-a-timestamp"
            else:
                document["unexpected"] = "synthetic"
                target_path.write_text(json.dumps(document), encoding="utf-8")
            if case != "target_unknown_key":
                target_path.write_text(json.dumps(document), encoding="utf-8")
        corrupted_path = target_path
        nurture_store = NurtureStore(state.root)
        read = lambda: nurture_store._read_target_document(
            target_path, state.account.id, state.preset.id
        )
    elif case.startswith("content_"):
        document = json.loads(content_path.read_text(encoding="utf-8"))
        document["candidates"][0]["source_fingerprint"] = "synthetic-invalid-fingerprint"
        content_path.write_text(json.dumps(document), encoding="utf-8")
        corrupted_path = content_path
        nurture_store = NurtureStore(state.root)
        read = lambda: nurture_store._read_content_document(
            content_path, state.account.id, state.preset.id
        )
    elif case.startswith("insight_"):
        insight = state.insight
        corrupted_path = (
            state.root
            / "nurture"
            / "insights"
            / str(state.account.id)
            / state.preset.id
            / insight.content_fingerprint
            / f"{insight.snapshot_id}.json"
        )
        document = json.loads(corrupted_path.read_text(encoding="utf-8"))
        document["likes"] = -1
        corrupted_path.write_text(json.dumps(document), encoding="utf-8")
        nurture_store = NurtureStore(state.root)
        read = lambda: nurture_store._read_insights_snapshot(corrupted_path, insight.snapshot_id)
    elif case.startswith("recurrence_"):
        if case == "recurrence_duplicate":
            recurrence_path.write_text('{"version":1,"version":1}', encoding="utf-8")
        else:
            document = json.loads(recurrence_path.read_text(encoding="utf-8"))
            document["updated_at"] = "synthetic-not-a-timestamp"
            recurrence_path.write_text(json.dumps(document), encoding="utf-8")
        corrupted_path = recurrence_path
        recurrence_store = LocalRecurrenceStore(state.root, LocalAccountStore(state.root))
        read = lambda: recurrence_store.get(state.recurrence.id)
    else:
        workflow_path = recurrence_dir / "workflow.json"
        workflow_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "account": state.account.alias,
                    "steps": [{"action": "post_text", "text": "synthetic mutation"}],
                }
            ),
            encoding="utf-8",
        )
        corrupted_path = workflow_path
        recurrence_store = LocalRecurrenceStore(state.root, LocalAccountStore(state.root))
        read = lambda: recurrence_store.load_workflow(recurrence_store.get(state.recurrence.id))

    corrupted_bytes = corrupted_path.read_bytes()
    before_failure = _inventory(state.root)
    with pytest.raises(Exception) as caught:
        read()
    if getattr(caught.value, "code", None) != expected_code:
        pytest.fail("synthetic corrupted state returned an unexpected safe error code")
    if corrupted_path.read_bytes() != corrupted_bytes or _inventory(state.root) != before_failure:
        pytest.fail("synthetic state parser rewrote a rejected document")


_READ_ONLY_CHILD = r"""
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID


def _forbidden(*args, **kwargs):
    raise RuntimeError("runtime or lock used during read-only inspection")


def _matches_operation(operation, expected):
    return (
        str(operation.id) == expected["id"]
        and str(operation.account_id) == expected["account_id"]
        and operation.kind == expected["kind"]
        and operation.phase == expected["phase"]
        and operation.container_id == expected["container_id"]
        and operation.media_id == expected["media_id"]
        and operation.outcome_code == expected["outcome_code"]
        and list(operation.child_container_ids) == expected["child_container_ids"]
        and operation.action == expected["action"]
    )


stage = "startup"


def main():
    global stage
    root = Path(sys.argv[1])
    expected = json.load(sys.stdin)
    stage = "state-store imports"
    from threads_platform.locks.filesystem import FilesystemProcessLock
    from threads_platform.standalone.accounts import LocalAccountStore
    from threads_platform.standalone.mutations import LocalOperationStore
    from threads_platform.standalone.nurture import get_nurture_preset
    from threads_platform.standalone.nurture_store import NurtureStore
    from threads_platform.standalone.recurrences import LocalRecurrenceStore

    stage = "runtime guard imports"
    from threads_platform.standalone.api import LocalThreadsApiRuntime
    from threads_platform.standalone.mutations import LocalThreadsMutationRuntime
    from threads_platform.standalone.runtime import LocalRuntime
    from threads_platform.standalone.nurture_runner import _own_content_is_due, _surface_eligible

    FilesystemProcessLock.acquire = _forbidden
    LocalThreadsApiRuntime.__init__ = _forbidden
    LocalThreadsMutationRuntime.__init__ = _forbidden
    LocalRuntime.__init__ = _forbidden
    socket.create_connection = _forbidden

    stage = "account"
    account_store = LocalAccountStore(root)
    account = account_store.get(expected["account"]["alias"])
    account_again = account_store.get(expected["account"]["alias"])
    account_match = (
        str(account.id) == expected["account"]["id"]
        and account.alias == expected["account"]["alias"]
        and account.credential_ref == expected["account"]["credential_ref"]
        and account == account_again
    )

    stage = "operation journals"
    operation_store = LocalOperationStore(root)
    operations = {}
    operations_match = True
    repeat_inspection_stable = True
    for item in expected["operations"]:
        operation_id = UUID(item["id"])
        operation = operation_store.get(operation_id)
        operations[item["name"]] = operation
        operations_match = operations_match and _matches_operation(operation, item)
        if item["phase"] in {"PUBLISH_REQUESTED", "MUTATION_REQUESTED", "AMBIGUOUS"}:
            repeat_inspection_stable = (
                repeat_inspection_stable and operation == operation_store.get(operation_id)
            )

    stage = "nurture receipts and state"
    nurture_store = NurtureStore(root)
    completed_id = UUID(expected["completed_run_id"])
    active_id = UUID(expected["active_run_id"])
    runs_root = root / "nurture" / "runs" / expected["account"]["id"]
    completed = nurture_store._read_run(runs_root / "completed" / f"{completed_id}.json", completed_id)
    active = nurture_store._read_run(runs_root / "active" / f"{active_id}.json", active_id)
    receipt_links_match = (
        completed.outcome == "SUCCESS"
        and str(completed.account_id) == expected["account"]["id"]
        and completed.preset_id == expected["preset_id"]
        and sorted(str(value) for value in completed.operation_ids)
        == expected["receipt_operation_ids"]
    )
    active_receipt_stays_running = active.outcome == "RUNNING" and active.finished_at is None

    target_path = (
        root
        / "nurture"
        / "state"
        / expected["account"]["id"]
        / f"{expected['preset_id']}.json"
    )
    targets = nurture_store._read_target_document(
        target_path,
        UUID(expected["account"]["id"]),
        expected["preset_id"],
    )
    targets_again = nurture_store._read_target_document(
        target_path,
        UUID(expected["account"]["id"]),
        expected["preset_id"],
    )
    target_by_fingerprint = {item.fingerprint: item for item in targets.targets}
    target_states_match = targets == targets_again and len(target_by_fingerprint) == len(expected["targets"])
    for item in expected["targets"].values():
        target = target_by_fingerprint.get(item["fingerprint"])
        target_states_match = target_states_match and target is not None and (
            target.action_state == item["action_state"]
            and str(target.last_run_id) == item["last_run_id"]
            and (None if target.last_operation_id is None else str(target.last_operation_id))
            == item["last_operation_id"]
        )

    content_path = (
        root
        / "nurture"
        / "content"
        / expected["account"]["id"]
        / f"{expected['preset_id']}.json"
    )
    content = nurture_store._read_content_document(
        content_path,
        UUID(expected["account"]["id"]),
        expected["preset_id"],
    )
    content_again = nurture_store._read_content_document(
        content_path,
        UUID(expected["account"]["id"]),
        expected["preset_id"],
    )
    content_by_id = {str(item.candidate_id): item for item in content.candidates}
    content_states_match = content == content_again and len(content_by_id) == len(expected["content"])
    for item in expected["content"].values():
        record = content_by_id.get(item["candidate_id"])
        content_states_match = content_states_match and record is not None and (
            record.source_fingerprint == item["source_fingerprint"]
            and record.draft_fingerprint == item["draft_fingerprint"]
            and record.publication_state == item["publication_state"]
            and str(record.last_run_id) == item["last_run_id"]
            and (None if record.operation_id is None else str(record.operation_id))
            == item["operation_id"]
        )

    insight_expected = expected["insight"]
    insight_path = (
        root
        / "nurture"
        / "insights"
        / expected["account"]["id"]
        / expected["preset_id"]
        / insight_expected["content_fingerprint"]
        / f"{insight_expected['snapshot_id']}.json"
    )
    insight = nurture_store._read_insights_snapshot(
        insight_path,
        insight_expected["snapshot_id"],
    )
    insights_fingerprint_and_operation_link_match = (
        insight.snapshot_id == insight_expected["snapshot_id"]
        and insight.content_fingerprint == insight_expected["content_fingerprint"]
        and insight.source_fingerprint == insight_expected["source_fingerprint"]
        and insight.draft_fingerprint == insight_expected["draft_fingerprint"]
        and str(insight.operation_id) == insight_expected["operation_id"]
        and [insight.likes, insight.replies, insight.reposts, insight.quotes]
        == insight_expected["metrics"]
    )

    stage = "workflow and recurrence"
    recurrence_store = LocalRecurrenceStore(root, account_store)
    recurrence_expected = expected["recurrence"]
    recurrence = recurrence_store.get(recurrence_expected["id"])
    recurrence_again = recurrence_store.get(recurrence_expected["id"])
    plan = recurrence_store.load_workflow(recurrence)
    plan_again = recurrence_store.load_workflow(recurrence_again)
    snapshot_path = root / "recurrences" / recurrence.id / "workflow.json"
    recurrence_snapshot_matches = (
        recurrence == recurrence_again
        and recurrence.status == recurrence_expected["status"] == "DISABLED"
        and recurrence.account == recurrence_expected["account"]
        and recurrence.interval_seconds == recurrence_expected["interval_seconds"]
        and recurrence.anchor_at.isoformat() == recurrence_expected["anchor_at"]
        and recurrence.next_due_at.isoformat() == recurrence_expected["next_due_at"]
        and snapshot_path.read_bytes().hex() == expected["workflow_hex"]
        and plan == plan_again
        and plan.version == 1
        and plan.account == expected["account"]["alias"]
        and len(plan.steps) == 1
        and type(plan.steps[0]).__name__ == "QuotaStep"
    )

    stage = "nurture suppression rules"
    preset = get_nurture_preset(expected["preset_id"])
    now = datetime.fromisoformat(expected["now"])
    target_by_name = {
        name: target_by_fingerprint[item["fingerprint"]]
        for name, item in expected["targets"].items()
    }
    cooldown_now = now + timedelta(seconds=preset.seen_cooldown_seconds - 1)
    confirmed_target_within_cooldown_not_selected = not _surface_eligible(
        target_by_name["confirmed"], preset.seen_cooldown_seconds, cooldown_now
    )
    pending_and_ambiguous_targets_suppressed = all(
        not _surface_eligible(target_by_name[name], preset.seen_cooldown_seconds, cooldown_now)
        for name in ("pending", "ambiguous")
    )
    published_record = content_by_id[expected["content"]["published"]["candidate_id"]]
    required_content_interval = max(
        preset.own_content_cooldown_seconds,
        preset.own_content_due_interval_seconds,
    )
    published_content_not_due = not _own_content_is_due(
        content.candidates,
        preset,
        published_record.last_action_at + timedelta(seconds=required_content_interval - 1),
    )

    operation_kinds = {operation.kind for operation in operations.values()}
    operation_phases = {operation.phase for operation in operations.values()}
    summary = {
        "passed": True,
        "account_match": account_match,
        "operations_match": operations_match and len(operations) == len(expected["operations"]),
        "operation_kind_count": len(operation_kinds),
        "all_journal_kinds": len(operation_kinds) == 7,
        "required_journal_phases": {
            "PUBLISH_REQUESTED",
            "MUTATION_REQUESTED",
            "PUBLISHED",
            "CONFIRMED",
            "FAILED_FINAL",
            "AMBIGUOUS",
        }.issubset(operation_phases),
        "carousel_children_linked": operations["carousel_published"].child_container_ids
        == ("synthetic-carousel-child-one", "synthetic-carousel-child-two"),
        "completed_receipt_links_match": receipt_links_match,
        "active_receipt_stays_running": active_receipt_stays_running,
        "target_states_and_links_match": target_states_match,
        "content_states_and_links_match": content_states_match,
        "insights_fingerprint_and_operation_link_match": insights_fingerprint_and_operation_link_match,
        "recurrence_snapshot_matches": recurrence_snapshot_matches,
        "confirmed_target_within_cooldown_not_selected": confirmed_target_within_cooldown_not_selected,
        "pending_and_ambiguous_targets_suppressed": pending_and_ambiguous_targets_suppressed,
        "published_content_within_interval_not_due": published_content_not_due,
        "repeat_inspection_stable": repeat_inspection_stable,
    }
    summary["passed"] = all(value is True for key, value in summary.items() if key != "operation_kind_count")
    print(json.dumps(summary, sort_keys=True))


try:
    main()
except Exception:
    print(json.dumps({"passed": False, "failed_stage": stage}))
    raise SystemExit(1)
"""
