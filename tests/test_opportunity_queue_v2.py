from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode
from uuid import UUID

import pytest

from threads_platform.browser.read_adapters import (
    SyntheticBrowserConversationAdapter,
    SyntheticBrowserSearchAdapter,
)
from threads_platform.browser.synthetic_html_readers import (
    SyntheticBrowserSessionContext,
    SyntheticHtmlBrowserReader,
    SyntheticHtmlDocument,
)
from threads_platform.standalone.browser_opportunity_bridge import (
    AccountScopedConversationResult,
    AccountScopedSearchResult,
    BrowserOpportunityProposal,
    BrowserOpportunityProposalBatch,
    ConversationObservationEvidence,
    build_browser_opportunity_proposals,
)
from threads_platform.standalone.manual_opportunities import (
    ManualOpportunity,
    ManualOpportunityIntake,
)
from threads_platform.standalone.opportunity_queue import OpportunityQueueStore
from threads_platform.standalone.opportunity_queue_v2 import (
    OpportunityQueueV2Error,
    OpportunityQueueV2Store,
)

_ACCOUNT_ONE = UUID("00000000-0000-4000-8000-000000000001")
_ACCOUNT_TWO = UUID("00000000-0000-4000-8000-000000000002")
_QUERY = "design role"
_ROOT = "https://www.threads.com/@author/post/root_1"
_REPLY = "https://www.threads.com/@reply_author/post/reply_1"


def _target(post_id: str, author: str = "author") -> str:
    return f"https://www.threads.com/@{author}/post/{post_id}"


def _post(
    username: str,
    post_id: str,
    *,
    text: str | None = None,
    parent_ref: str | None = None,
    truncated: bool = False,
) -> str:
    text_marker = ""
    if text is not None:
        text_marker = (
            '<p role="paragraph" aria-label="Post text" '
            f'data-truncated="{str(truncated).lower()}">{escape(text)}</p>'
        )
    parent_marker = ""
    if parent_ref is not None:
        parent_marker = (
            '<a role="link" aria-label="Replying to" '
            f'href="{escape(parent_ref, quote=True)}">Reply target</a>'
        )
    return (
        '<article role="article">'
        '<a role="link" aria-label="Post permalink" '
        f'href="/@{username}/post/{post_id}">Open post</a>'
        f'<a role="link" aria-label="Author" href="/@{username}/">@{username}</a>'
        f"{text_marker}{parent_marker}</article>"
    )


def _document(url: str, main: str, account_id: UUID) -> SyntheticHtmlDocument:
    return SyntheticHtmlDocument(
        url=url,
        html=(
            "<html><head>"
            f'<meta name="synthetic-account-id" content="{account_id}">'
            "</head><body>"
            f"{main}</body></html>"
        ),
    )


def _synthetic_batch(
    account_id: UUID = _ACCOUNT_ONE,
    *,
    query: str = _QUERY,
    root_text: str | None = "Synthetic root excerpt",
    replies: tuple[ConversationObservationEvidence, ...] = (),
    context_status: str = "UNKNOWN",
    conversation_truncated: bool | None = None,
    search_truncated: bool = False,
    search_observation_truncated: bool = False,
) -> BrowserOpportunityProposalBatch:
    root_observation = (
        (
            ConversationObservationEvidence(
                target_permalink=_ROOT,
                author_marker="author",
                author_identity_status="UNVERIFIED",
                excerpt=root_text,
                position=0,
                parent_permalink=None,
                is_root=True,
                truncated=False,
            ),
        )
        if conversation_truncated is not None
        else ()
    )
    observations = (*root_observation, *replies)
    truncated = (
        search_truncated
        or search_observation_truncated
        or conversation_truncated is True
        or any(row.truncated for row in observations)
    )
    proposal = BrowserOpportunityProposal(
        account_uuid=account_id,
        target_permalink=_ROOT,
        source="SYNTHETIC_FIXTURE",
        context_provenance="SYNTHETIC_FIXTURE",
        author_marker="author",
        author_identity_status="UNVERIFIED",
        excerpt=root_text,
        position=0,
        search_context_status="UNKNOWN",
        context_status=cast(Literal["UNKNOWN", "PARTIAL", "COMPLETE"], context_status),
        review_state="NEEDS_CONTEXT",
        truncated=truncated,
        search_observation_truncated=search_observation_truncated,
        search_result_truncated=search_truncated,
        conversation_result_truncated=conversation_truncated,
        conversation_observations=observations,
        reason_codes=("AUTHOR_IDENTITY_UNVERIFIED",),
    )
    return BrowserOpportunityProposalBatch(
        account_uuid=account_id,
        query=query,
        result_version=1,
        source="SYNTHETIC_FIXTURE",
        search_result_truncated=search_truncated,
        proposals=(proposal,),
    )


def _reader_batch(
    *,
    account_id: UUID = _ACCOUNT_ONE,
    conversation_posts: tuple[str, ...] | None = None,
) -> BrowserOpportunityProposalBatch:
    query = _QUERY
    search_main = (
        '<main role="main" aria-label="Search results" '
        f'data-query="{query}">'
        f'<section role="region" aria-label="Search result batch">'
        f"{_post('author', 'root_1', text='A synthetic recruitment post')}"
        "</section></main>"
    )
    search_document = _document(
        f"https://www.threads.com/search?{urlencode({'q': query})}",
        search_main,
        account_id,
    )
    search_reader = SyntheticHtmlBrowserReader(
        search_document,
        SyntheticBrowserSessionContext(account_id),
    )
    search = asyncio.run(SyntheticBrowserSearchAdapter(search_reader).search(query))
    conversations = ()
    if conversation_posts is not None:
        conversation_main = (
            '<main role="main" aria-label="Conversation" '
            f'data-target="{_ROOT}" data-context-status="complete">'
            f"{''.join(conversation_posts)}</main>"
        )
        conversation_document = _document(_ROOT, conversation_main, account_id)
        conversation_reader = SyntheticHtmlBrowserReader(
            conversation_document,
            SyntheticBrowserSessionContext(account_id),
        )
        conversation = asyncio.run(
            SyntheticBrowserConversationAdapter(conversation_reader).read(_ROOT)
        )
        conversations = (AccountScopedConversationResult(account_id, _ROOT, conversation),)
    return build_browser_opportunity_proposals(
        AccountScopedSearchResult(account_id, search),
        expected_account_uuid=account_id,
        conversations=conversations,
    )


def _v1_path(root: Path, account_id: UUID = _ACCOUNT_ONE) -> Path:
    return root / "opportunity_queue_v1" / "accounts" / f"{account_id}.json"


def _v2_path(root: Path, account_id: UUID = _ACCOUNT_ONE) -> Path:
    return root / "opportunity_queue_v2" / "accounts" / f"{account_id}.json"


def _make_v1_queue(root: Path, account_id: UUID = _ACCOUNT_ONE) -> None:
    candidates = tuple(
        ManualOpportunity(
            input_position=index,
            source_line=index,
            target_url=_target(f"migrated_{index}"),
            author_username="author",
        )
        for index in range(1, 4)
    )
    v1 = OpportunityQueueStore(root)
    v1.import_candidates(account_id, ManualOpportunityIntake(candidates, ()))
    queue = v1.list(account_id)
    v1.select(
        account_id,
        queue.opportunities[0].id,
        context="Private operator context",
        update_context=True,
        context_completeness="COMPLETE",
    )
    v1.skip(account_id, queue.opportunities[2].id, reason="Operator skipped")


def test_synthetic_reader_bridge_v2_store_and_restart_preserve_source_evidence(
    tmp_path: Path,
) -> None:
    batch = _reader_batch(
        conversation_posts=(
            _post("author", "root_1", text="A synthetic recruitment post"),
            _post("reply_author", "reply_1", text="Synthetic reply", parent_ref=_ROOT),
        )
    )
    store = OpportunityQueueV2Store(tmp_path)
    summary = store.intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    restarted = OpportunityQueueV2Store(tmp_path).list(_ACCOUNT_ONE)

    assert (summary.added_count, summary.duplicate_count) == (1, 0)
    assert restarted.active_count == 1
    entry = restarted.opportunities[0]
    assert entry.source == "SYNTHETIC_FIXTURE"
    assert entry.target_url == _ROOT
    assert entry.author_marker == "author"
    assert entry.author_identity_status == "UNVERIFIED"
    assert entry.context is None
    assert entry.context_provenance == "SYNTHETIC_FIXTURE"
    assert entry.context_completeness == "UNKNOWN"
    assert entry.state == "NEEDS_CONTEXT"
    assert entry.source_evidence is not None
    assert entry.source_evidence.query == _QUERY
    assert entry.source_evidence.context_status == "COMPLETE"
    assert entry.source_evidence.position == 0
    assert entry.source_evidence.conversation_observations[1].parent_url == _ROOT
    assert entry.source_evidence.conversation_observations[1].excerpt == "Synthetic reply"
    assert not _v1_path(tmp_path).exists()


def test_synthetic_complete_fixture_needs_explicit_operator_context(tmp_path: Path) -> None:
    batch = _reader_batch(
        conversation_posts=(_post("author", "root_1", text="A synthetic recruitment post"),)
    )
    store = OpportunityQueueV2Store(tmp_path)
    store.intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    entry = store.list(_ACCOUNT_ONE).opportunities[0]
    assert entry.source_evidence is not None
    assert entry.source_evidence.context_status == "COMPLETE"
    assert entry.state == "NEEDS_CONTEXT"

    with pytest.raises(OpportunityQueueV2Error) as missing_assertion:
        store.set_context(
            _ACCOUNT_ONE,
            entry.id,
            "Operator reviewed context",
            completeness="COMPLETE",
        )
    assert missing_assertion.value.code == "OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID"

    source_evidence = entry.source_evidence
    ready = store.set_context(
        _ACCOUNT_ONE,
        entry.id,
        "Operator reviewed context",
        completeness="COMPLETE",
        operator_confirmed_complete=True,
    )
    reread = OpportunityQueueV2Store(tmp_path).get(_ACCOUNT_ONE, entry.id)
    assert ready.state == reread.state == "READY"
    assert reread.context_provenance == "OPERATOR_SUPPLIED"
    assert reread.context_completeness == "COMPLETE"
    assert reread.source == "SYNTHETIC_FIXTURE"
    assert reread.author_identity_status == "UNVERIFIED"
    assert reread.source_evidence == source_evidence


def test_manual_and_synthetic_provenance_are_distinct_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    manual = store.import_manual_urls(_ACCOUNT_ONE, (_target("manual_1"),))
    synthetic_batch = replace(
        _synthetic_batch(),
        proposals=(
            replace(_synthetic_batch().proposals[0], target_permalink=_target("synthetic_1")),
        ),
    )
    synthetic = store.intake_synthetic_proposals(_ACCOUNT_ONE, synthetic_batch)
    queue = store.list(_ACCOUNT_ONE)

    assert manual.added_count == synthetic.added_count == 1
    assert [entry.source for entry in queue.opportunities] == ["MANUAL_URL", "SYNTHETIC_FIXTURE"]
    assert queue.opportunities[0].source_evidence is None
    assert queue.opportunities[0].author_identity_status == "UNKNOWN"

    with pytest.raises(OpportunityQueueV2Error) as conflict:
        store.intake_synthetic_proposals(
            _ACCOUNT_ONE,
            replace(
                synthetic_batch,
                proposals=(
                    replace(synthetic_batch.proposals[0], target_permalink=_target("manual_1")),
                ),
            ),
        )
    assert conflict.value.code == "OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT"


def test_manual_url_normalization_duplicates_and_account_isolation(tmp_path: Path) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    tracking = f"{_target('manual_2')}?utm_source=fixture"
    imported = store.import_manual_urls(_ACCOUNT_ONE, (tracking, _target("manual_2")))
    isolated = store.import_manual_urls(_ACCOUNT_TWO, (_target("manual_2"),))

    assert (imported.added_count, imported.duplicate_count) == (1, 1)
    assert (isolated.added_count, isolated.duplicate_count) == (1, 0)
    assert len(store.list(_ACCOUNT_ONE).opportunities) == 1
    assert len(store.list(_ACCOUNT_TWO).opportunities) == 1
    assert _v2_path(tmp_path, _ACCOUNT_ONE) != _v2_path(tmp_path, _ACCOUNT_TWO)


@pytest.mark.parametrize("count", [0, 21])
def test_manual_import_bounds_reject_zero_and_excess(tmp_path: Path, count: int) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    targets = tuple(_target(f"bounded_{index}") for index in range(count))
    with pytest.raises(OpportunityQueueV2Error) as error:
        store.import_manual_urls(_ACCOUNT_ONE, targets)
    assert error.value.code == "OPPORTUNITY_QUEUE_V2_LIMIT_REACHED"
    assert not _v2_path(tmp_path).exists()


def test_twenty_active_limit_and_forty_retained_limit(tmp_path: Path) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    targets = tuple(_target(f"capacity_{index}") for index in range(20))
    summary = store.import_manual_urls(_ACCOUNT_ONE, targets)
    assert (summary.active_count, summary.total_count) == (20, 20)
    with pytest.raises(OpportunityQueueV2Error) as active_error:
        store.import_manual_urls(_ACCOUNT_ONE, (_target("active_excess"),))
    assert active_error.value.code == "OPPORTUNITY_QUEUE_V2_LIMIT_REACHED"

    first = store.list(_ACCOUNT_ONE)
    for entry in first.opportunities:
        store.skip(_ACCOUNT_ONE, entry.id)
    second = store.import_manual_urls(
        _ACCOUNT_ONE,
        tuple(_target(f"retained_{index}") for index in range(20)),
    )
    assert (second.active_count, second.total_count) == (20, 40)
    for entry in store.list(_ACCOUNT_ONE).opportunities[20:]:
        store.skip(_ACCOUNT_ONE, entry.id)
    with pytest.raises(OpportunityQueueV2Error) as retained_error:
        store.import_manual_urls(_ACCOUNT_ONE, (_target("retained_excess"),))
    assert retained_error.value.code == "OPPORTUNITY_QUEUE_V2_LIMIT_REACHED"
    assert len(store.list(_ACCOUNT_ONE).opportunities) == 40


def test_synthetic_canonical_duplicates_are_idempotent_but_conflicts_reject(
    tmp_path: Path,
) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    batch = _synthetic_batch()
    proposal = batch.proposals[0]
    duplicate_batch = replace(batch, proposals=(proposal, proposal))
    summary = store.intake_synthetic_proposals(_ACCOUNT_ONE, duplicate_batch)
    repeated = store.intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    assert (summary.added_count, summary.duplicate_count) == (1, 1)
    assert (repeated.added_count, repeated.duplicate_count) == (0, 1)

    contradictory = replace(batch, proposals=(proposal, replace(proposal, excerpt="Other text")))
    with pytest.raises(OpportunityQueueV2Error) as error:
        OpportunityQueueV2Store(tmp_path / "other").intake_synthetic_proposals(
            _ACCOUNT_ONE,
            contradictory,
        )
    assert error.value.code == "OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT"


def test_synthetic_batch_version_provenance_account_and_author_are_validated(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[BrowserOpportunityProposalBatch, UUID, str], ...] = (
        (
            _synthetic_batch(),
            _ACCOUNT_TWO,
            "OPPORTUNITY_QUEUE_V2_ACCOUNT_MISMATCH",
        ),
    )
    for batch, account, code in cases:
        with pytest.raises(OpportunityQueueV2Error) as error:
            OpportunityQueueV2Store(tmp_path / str(account)).intake_synthetic_proposals(
                account,
                batch,
            )
        assert error.value.code == code

    bad_version = _synthetic_batch()
    object.__setattr__(bad_version, "result_version", 2)
    with pytest.raises(OpportunityQueueV2Error) as version_error:
        OpportunityQueueV2Store(tmp_path / "version").intake_synthetic_proposals(
            _ACCOUNT_ONE,
            bad_version,
        )
    assert version_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"
    assert not _v2_path(tmp_path / "version").exists()

    bad_source = _synthetic_batch()
    object.__setattr__(bad_source, "source", "BROWSER_UI")
    with pytest.raises(OpportunityQueueV2Error) as provenance_error:
        OpportunityQueueV2Store(tmp_path / "source").intake_synthetic_proposals(
            _ACCOUNT_ONE,
            bad_source,
        )
    assert provenance_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE"

    bad_author = _synthetic_batch()
    object.__setattr__(bad_author.proposals[0], "author_marker", "different")
    with pytest.raises(OpportunityQueueV2Error) as author_error:
        OpportunityQueueV2Store(tmp_path / "author").intake_synthetic_proposals(
            _ACCOUNT_ONE,
            bad_author,
        )
    assert author_error.value.code == "OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT"


def test_partial_missing_parent_and_truncation_are_preserved(tmp_path: Path) -> None:
    partial_batch = _reader_batch(
        conversation_posts=(
            _post("author", "root_1", text="A synthetic recruitment post"),
            _post("reply_author", "reply_1", text="Reply without parent evidence"),
        )
    )
    store = OpportunityQueueV2Store(tmp_path)
    store.intake_synthetic_proposals(_ACCOUNT_ONE, partial_batch)
    entry = store.list(_ACCOUNT_ONE).opportunities[0]

    assert entry.state == "NEEDS_CONTEXT"
    assert entry.source_evidence is not None
    assert entry.source_evidence.context_status == "PARTIAL"
    assert entry.source_evidence.conversation_observations[1].parent_url is None

    truncated = _synthetic_batch(
        search_observation_truncated=True,
        search_truncated=True,
    )
    store.intake_synthetic_proposals(
        _ACCOUNT_ONE,
        replace(
            truncated,
            proposals=(replace(truncated.proposals[0], target_permalink=_target("truncated_1")),),
        ),
    )
    truncated_entry = store.list(_ACCOUNT_ONE).opportunities[1]
    assert truncated_entry.source_evidence is not None
    assert truncated_entry.source_evidence.truncated is True
    assert truncated_entry.source_evidence.search_observation_truncated is True
    assert truncated_entry.source_evidence.search_result_truncated is True


def test_invalid_target_and_mutated_provenance_never_write(tmp_path: Path) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    batch = _synthetic_batch()
    object.__setattr__(batch.proposals[0], "target_permalink", f"{_ROOT}?utm_source=not-canonical")
    with pytest.raises(OpportunityQueueV2Error) as error:
        store.intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    assert error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"
    assert not _v2_path(tmp_path).exists()

    batch = _synthetic_batch()
    object.__setattr__(batch.proposals[0], "context_provenance", "OPERATOR_SUPPLIED")
    with pytest.raises(OpportunityQueueV2Error) as source_error:
        store.intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    assert source_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE"


def test_invalid_parent_evidence_and_context_type_fail_closed(tmp_path: Path) -> None:
    root = ConversationObservationEvidence(
        target_permalink=_ROOT,
        author_marker="author",
        author_identity_status="UNVERIFIED",
        excerpt="Root",
        position=0,
        parent_permalink=None,
        is_root=True,
        truncated=False,
    )
    reply = ConversationObservationEvidence(
        target_permalink=_REPLY,
        author_marker="reply_author",
        author_identity_status="UNVERIFIED",
        excerpt="Reply",
        position=1,
        parent_permalink=_REPLY,
        is_root=False,
        truncated=False,
    )
    batch = _synthetic_batch(
        replies=(reply,),
        context_status="PARTIAL",
        conversation_truncated=False,
    )
    proposal = replace(batch.proposals[0], conversation_observations=(root, reply))
    batch = replace(batch, proposals=(proposal,))
    with pytest.raises(OpportunityQueueV2Error) as parent_error:
        OpportunityQueueV2Store(tmp_path).intake_synthetic_proposals(_ACCOUNT_ONE, batch)
    assert parent_error.value.code == "OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT"

    context_root = tmp_path / "context"
    context_root.mkdir()
    entry = OpportunityQueueV2Store(context_root).import_manual_urls(
        _ACCOUNT_ONE,
        (_target("context_1"),),
    )
    stored = OpportunityQueueV2Store(context_root).list(_ACCOUNT_ONE).opportunities[0]
    with pytest.raises(OpportunityQueueV2Error) as context_error:
        OpportunityQueueV2Store(context_root).set_context(
            _ACCOUNT_ONE,
            stored.id,
            "operator context",
            completeness=cast(object, ["COMPLETE"]),
        )
    assert context_error.value.code == "OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID"
    assert entry.added_count == 1


def test_v1_migration_preview_is_bounded_read_only_and_confirmation_bound(
    tmp_path: Path,
) -> None:
    _make_v1_queue(tmp_path)
    original = _v1_path(tmp_path).read_bytes()
    preview = OpportunityQueueV2Store(tmp_path).preview_v1_migration(_ACCOUNT_ONE)
    assert preview.source_exists is True
    assert preview.v1_total_count == 3
    assert preview.v1_active_count == 2
    assert preview.v1_ready_count == 1
    assert preview.v1_needs_context_count == 1
    assert preview.v1_skipped_count == 1
    assert preview.v2_target_exists is False
    assert preview.confirmation_token is not None
    assert "Private operator context" not in repr(preview)
    assert _v1_path(tmp_path).read_bytes() == original
    assert not _v2_path(tmp_path).exists()

    store = OpportunityQueueV2Store(tmp_path)
    with pytest.raises(OpportunityQueueV2Error) as confirmation_error:
        store.apply_v1_migration(_ACCOUNT_ONE, confirmation="")
    assert confirmation_error.value.code == "OPPORTUNITY_QUEUE_V2_CONFIRMATION_REQUIRED"
    assert not _v2_path(tmp_path).exists()

    v1 = OpportunityQueueStore(tmp_path).list(_ACCOUNT_ONE)
    result = store.apply_v1_migration(
        _ACCOUNT_ONE,
        confirmation=preview.confirmation_token,
    )
    v2 = OpportunityQueueV2Store(tmp_path).list(_ACCOUNT_ONE)
    assert result.migrated_count == 3
    assert result.active_count == 2
    assert _v1_path(tmp_path).read_bytes() == original
    assert len(v1.opportunities) == len(v2.opportunities) == 3
    for old, new in zip(v1.opportunities, v2.opportunities, strict=True):
        assert new.id == old.id
        assert new.target_url == old.target_url
        assert new.source == "MANUAL_URL"
        assert new.author_marker is None
        assert new.author_identity_status == "UNKNOWN"
        assert new.source_evidence is None
        assert new.context == old.context
        assert new.context_provenance == old.context_provenance
        assert new.context_completeness == old.context_completeness
        assert new.created_at == old.created_at
        assert new.updated_at == old.updated_at
        assert new.state == old.state
        assert new.skip_reason == old.skip_reason


def test_migration_rejects_missing_source_target_collision_and_stale_preview(
    tmp_path: Path,
) -> None:
    empty_root = tmp_path / "missing"
    empty_root.mkdir()
    store = OpportunityQueueV2Store(empty_root)
    preview = store.preview_v1_migration(_ACCOUNT_ONE)
    assert preview.source_exists is False
    assert preview.confirmation_token is None
    with pytest.raises(OpportunityQueueV2Error) as missing:
        store.apply_v1_migration(_ACCOUNT_ONE, confirmation="anything")
    assert missing.value.code == "OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_MISSING"

    collision_root = tmp_path / "collision"
    collision_root.mkdir()
    _make_v1_queue(collision_root)
    v2 = OpportunityQueueV2Store(collision_root)
    v2.import_manual_urls(_ACCOUNT_ONE, (_target("already_v2"),))
    collision_preview = v2.preview_v1_migration(_ACCOUNT_ONE)
    assert collision_preview.v2_target_exists is True
    confirmation = f"MIGRATE_V1_TO_V2:{_ACCOUNT_ONE}:{collision_preview.source_sha256}"
    existing_v2_bytes = _v2_path(collision_root).read_bytes()
    with pytest.raises(OpportunityQueueV2Error) as collision:
        v2.apply_v1_migration(_ACCOUNT_ONE, confirmation=confirmation)
    assert collision.value.code == "OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS"
    assert _v2_path(collision_root).read_bytes() == existing_v2_bytes

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    _make_v1_queue(stale_root)
    stale_store = OpportunityQueueV2Store(stale_root)
    stale_preview = stale_store.preview_v1_migration(_ACCOUNT_ONE)
    v1_queue = OpportunityQueueStore(stale_root).list(_ACCOUNT_ONE)
    OpportunityQueueStore(stale_root).skip(_ACCOUNT_ONE, v1_queue.opportunities[0].id)
    with pytest.raises(OpportunityQueueV2Error) as changed:
        stale_store.apply_v1_migration(
            _ACCOUNT_ONE,
            confirmation=stale_preview.confirmation_token,
        )
    assert changed.value.code == "OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_CHANGED"
    assert not _v2_path(stale_root).exists()


def test_migration_staging_failure_rolls_back_without_changing_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_v1_queue(tmp_path)
    source_before = _v1_path(tmp_path).read_bytes()
    preview = OpportunityQueueV2Store(tmp_path).preview_v1_migration(_ACCOUNT_ONE)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic atomic install failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OpportunityQueueV2Error) as error:
        OpportunityQueueV2Store(tmp_path).apply_v1_migration(
            _ACCOUNT_ONE,
            confirmation=preview.confirmation_token,
        )
    assert error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"
    assert _v1_path(tmp_path).read_bytes() == source_before
    assert not _v2_path(tmp_path).exists()
    assert not list(_v2_path(tmp_path).parent.glob(".*.tmp"))


def test_malformed_v1_and_v2_documents_fail_closed(tmp_path: Path) -> None:
    v1_path = _v1_path(tmp_path)
    v1_path.parent.mkdir(parents=True)
    v1_path.write_text(
        '{"version":1,"version":1,"account_uuid":"00000000-0000-4000-8000-000000000001",'
        '"opportunities":[]}',
        encoding="utf-8",
    )
    with pytest.raises(OpportunityQueueV2Error) as v1_error:
        OpportunityQueueV2Store(tmp_path).preview_v1_migration(_ACCOUNT_ONE)
    assert v1_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"

    v1_path.unlink()
    store = OpportunityQueueV2Store(tmp_path)
    store.import_manual_urls(_ACCOUNT_ONE, (_target("bad_json"),))
    v2_path = _v2_path(tmp_path)
    v2_path.write_text(
        '{"version":2,"version":2,"account_uuid":"00000000-0000-4000-8000-000000000001",'
        '"opportunities":[]}',
        encoding="utf-8",
    )
    with pytest.raises(OpportunityQueueV2Error) as v2_error:
        store.list(_ACCOUNT_ONE)
    assert v2_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"


def test_wrong_version_mutated_fixture_state_and_oversized_file_reject(
    tmp_path: Path,
) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    store.import_manual_urls(_ACCOUNT_ONE, (_target("tamper_1"),))
    path = _v2_path(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OpportunityQueueV2Error) as version_error:
        store.list(_ACCOUNT_ONE)
    assert version_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"

    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = 2
    document["opportunities"][0]["state"] = "READY"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OpportunityQueueV2Error) as state_error:
        store.list(_ACCOUNT_ONE)
    assert state_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"

    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(OpportunityQueueV2Error) as size_error:
        store.list(_ACCOUNT_ONE)
    assert size_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"


def test_persisted_v2_rejects_wrong_account_and_browser_source(tmp_path: Path) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    store.import_manual_urls(_ACCOUNT_ONE, (_target("account_tamper"),))
    path = _v2_path(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["account_uuid"] = str(_ACCOUNT_TWO)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OpportunityQueueV2Error) as account_error:
        store.list(_ACCOUNT_ONE)
    assert account_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"

    document["account_uuid"] = str(_ACCOUNT_ONE)
    document["opportunities"][0]["source"] = "BROWSER_UI"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OpportunityQueueV2Error) as source_error:
        store.list(_ACCOUNT_ONE)
    assert source_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE"


def test_atomic_write_failure_preserves_previous_queue_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    store.import_manual_urls(_ACCOUNT_ONE, (_target("atomic_1"),))
    original = _v2_path(tmp_path).read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OpportunityQueueV2Error) as error:
        store.import_manual_urls(_ACCOUNT_ONE, (_target("atomic_2"),))
    assert error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"
    assert _v2_path(tmp_path).read_bytes() == original


def test_account_lock_contention_and_symlink_directory_fail_closed(tmp_path: Path) -> None:
    store = OpportunityQueueV2Store(tmp_path)
    with store.account_lock(_ACCOUNT_ONE), pytest.raises(OpportunityQueueV2Error) as busy:
        OpportunityQueueV2Store(tmp_path).list(_ACCOUNT_ONE)
    assert busy.value.code == "OPPORTUNITY_QUEUE_V2_BUSY"

    with store.account_lock(_ACCOUNT_ONE):
        other_account = OpportunityQueueV2Store(tmp_path).import_manual_urls(
            _ACCOUNT_TWO,
            (_target("independent_lock"),),
        )
    assert other_account.added_count == 1

    redirect = tmp_path / "redirect"
    redirect.mkdir()
    namespace = tmp_path / "symlink-root" / "opportunity_queue_v2"
    namespace.parent.mkdir()
    try:
        os.symlink(redirect, namespace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    with pytest.raises(OpportunityQueueV2Error) as symlink_error:
        OpportunityQueueV2Store(namespace.parent).import_manual_urls(
            _ACCOUNT_ONE,
            (_target("symlink_1"),),
        )
    assert symlink_error.value.code == "OPPORTUNITY_QUEUE_V2_INVALID"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_queue_directory_and_file_use_private_mode_bits(tmp_path: Path) -> None:
    OpportunityQueueV2Store(tmp_path).import_manual_urls(_ACCOUNT_ONE, (_target("private_1"),))
    assert os.stat(_v2_path(tmp_path).parent.parent).st_mode & 0o777 == 0o700
    assert os.stat(_v2_path(tmp_path)).st_mode & 0o777 == 0o600


def test_store_bridge_and_migration_make_no_network_or_browser_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected network, process, or browser effect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    store = OpportunityQueueV2Store(tmp_path)
    store.intake_synthetic_proposals(_ACCOUNT_ONE, _synthetic_batch())
    assert store.list(_ACCOUNT_ONE).opportunities[0].source == "SYNTHETIC_FIXTURE"
    assert store.preview_v1_migration(_ACCOUNT_TWO).source_exists is False
