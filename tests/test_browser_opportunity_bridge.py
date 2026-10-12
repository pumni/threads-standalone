from __future__ import annotations

import asyncio
import builtins
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode
from uuid import UUID

import pytest

from threads_platform.browser.capabilities import (
    BrowserConversationItemResultV1,
    BrowserConversationResultV1,
    BrowserSearchItemResultV1,
    BrowserSearchResultV1,
)
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
    BrowserOpportunityBridgeError,
    BrowserOpportunityProposal,
    BrowserOpportunityProposalBatch,
    build_browser_opportunity_proposals,
)

_ACCOUNT_ONE = UUID("00000000-0000-4000-8000-000000000001")
_ACCOUNT_TWO = UUID("00000000-0000-4000-8000-000000000002")
_QUERY = "design recruitment"
_ROOT = "https://www.threads.com/@author/post/root_1"
_REPLY = "https://www.threads.com/@reply_author/post/reply_1"
_TestSource = Literal["SYNTHETIC_FIXTURE", "MANUAL_URL", "BROWSER_UI"]
_TestContext = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]


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
        '<a role="link" aria-label="Author" '
        f'href="/@{username}/">@{username}</a>'
        f"{text_marker}{parent_marker}</article>"
    )


def _document(url: str, main: str, account_uuid: UUID) -> SyntheticHtmlDocument:
    return SyntheticHtmlDocument(
        url=url,
        html=(
            "<html><head>"
            f'<meta name="synthetic-account-id" content="{account_uuid}">'
            "</head><body>"
            f"{main}</body></html>"
        ),
    )


def _search_result(
    posts: Sequence[str],
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
) -> BrowserSearchResultV1:
    cards = "".join(posts)
    main = (
        '<main role="main" aria-label="Search results" '
        f'data-query="{escape(_QUERY, quote=True)}">'
        '<section role="region" aria-label="Search result batch">'
        f"{cards}</section></main>"
    )
    document = _document(
        f"https://www.threads.com/search?{urlencode({'q': _QUERY})}",
        main,
        account_uuid,
    )
    reader = SyntheticHtmlBrowserReader(document, SyntheticBrowserSessionContext(account_uuid))
    return asyncio.run(SyntheticBrowserSearchAdapter(reader).search(_QUERY))


def _conversation_result(
    posts: Sequence[str],
    *,
    target: str = _ROOT,
    account_uuid: UUID = _ACCOUNT_ONE,
    context_status: str = "complete",
) -> BrowserConversationResultV1:
    main = (
        '<main role="main" aria-label="Conversation" '
        f'data-target="{escape(target, quote=True)}" '
        f'data-context-status="{escape(context_status, quote=True)}">'
        f"{''.join(posts)}</main>"
    )
    document = _document(target, main, account_uuid)
    reader = SyntheticHtmlBrowserReader(document, SyntheticBrowserSessionContext(account_uuid))
    return asyncio.run(SyntheticBrowserConversationAdapter(reader).read(target))


def _scoped_search(
    result: BrowserSearchResultV1,
    account_uuid: UUID = _ACCOUNT_ONE,
) -> AccountScopedSearchResult:
    return AccountScopedSearchResult(account_uuid=account_uuid, result=result)


def _build(
    result: BrowserSearchResultV1,
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
    conversations: Sequence[AccountScopedConversationResult] = (),
) -> BrowserOpportunityProposalBatch:
    return build_browser_opportunity_proposals(
        _scoped_search(result, account_uuid),
        expected_account_uuid=account_uuid,
        conversations=conversations,
    )


def _manual_search_result(
    items: Sequence[BrowserSearchItemResultV1],
    *,
    truncated: bool = False,
) -> BrowserSearchResultV1:
    return BrowserSearchResultV1(
        query=_QUERY,
        observations=tuple(items),
        truncated=truncated,
        source="SYNTHETIC_FIXTURE",
    )


def _search_item(
    target: str = _ROOT,
    *,
    author: str = "author",
    excerpt: str | None = "A synthetic post excerpt",
    position: int = 0,
    truncated: bool = False,
    source: _TestSource = "SYNTHETIC_FIXTURE",
) -> BrowserSearchItemResultV1:
    return BrowserSearchItemResultV1(
        thread_ref=target,
        author_username=author,
        text_excerpt=excerpt,
        position=position,
        source=source,
        context_status="UNKNOWN",
        truncated=truncated,
    )


def _conversation_model(
    observations: Sequence[BrowserConversationItemResultV1],
    *,
    root: str = _ROOT,
    status: _TestContext = "COMPLETE",
    truncated: bool = False,
    source: _TestSource = "SYNTHETIC_FIXTURE",
) -> BrowserConversationResultV1:
    return BrowserConversationResultV1(
        root_thread_ref=root,
        observations=tuple(observations),
        context_status=status,
        truncated=truncated,
        source=source,
    )


def _conversation_item(
    target: str,
    author: str,
    *,
    excerpt: str | None,
    position: int,
    parent: str | None = None,
    truncated: bool = False,
) -> BrowserConversationItemResultV1:
    return BrowserConversationItemResultV1(
        thread_ref=target,
        author_username=author,
        text_excerpt=excerpt,
        parent_ref=parent,
        position=position,
        truncated=truncated,
    )


class BoundedMemoryProposalSink:
    """Test sink matching S6's 20-active, account-isolated intake bound."""

    def __init__(self) -> None:
        self.by_account: dict[UUID, dict[str, BrowserOpportunityProposal]] = {}

    def add(self, batch: BrowserOpportunityProposalBatch) -> None:
        current = self.by_account.get(batch.account_uuid, {})
        additions: dict[str, BrowserOpportunityProposal] = {}
        for proposal in batch.proposals:
            if proposal.account_uuid != batch.account_uuid:
                raise ValueError("ACCOUNT_MISMATCH")
            if proposal.source != "SYNTHETIC_FIXTURE":
                raise ValueError("INVALID_PROVENANCE")
            previous = current.get(proposal.target_permalink) or additions.get(
                proposal.target_permalink
            )
            if previous is not None:
                if previous != proposal:
                    raise ValueError("CONTRADICTORY_DUPLICATE")
                continue
            additions[proposal.target_permalink] = proposal
        if len(current) + len(additions) > 20:
            raise ValueError("ACTIVE_LIMIT")
        self.by_account[batch.account_uuid] = {**current, **additions}


def test_synthetic_adapter_to_bridge_handles_zero_and_one_result() -> None:
    empty = _build(_search_result(()))
    single = _build(_search_result((_post("author", "root_1", text="A synthetic post excerpt"),)))

    assert empty.proposals == ()
    assert empty.source == "SYNTHETIC_FIXTURE"
    assert empty.search_result_truncated is False
    assert len(single.proposals) == 1
    proposal = single.proposals[0]
    assert proposal.account_uuid == _ACCOUNT_ONE
    assert proposal.target_permalink == _ROOT
    assert proposal.source == proposal.context_provenance == "SYNTHETIC_FIXTURE"
    assert proposal.author_marker == "author"
    assert proposal.author_identity_status == "UNVERIFIED"
    assert proposal.excerpt == "A synthetic post excerpt"
    assert proposal.position == 0
    assert proposal.context_status == "UNKNOWN"
    assert proposal.review_state == "NEEDS_CONTEXT"
    assert proposal.truncated is False
    assert "SYNTHETIC_SOURCE_REQUIRES_VERSIONED_QUEUE" in proposal.reason_codes


def test_twenty_and_excess_results_preserve_bounds_and_search_truncation() -> None:
    twenty = _build(_search_result(tuple(_post("author", f"post_{index}") for index in range(20))))
    excess = _build(_search_result(tuple(_post("author", f"post_{index}") for index in range(21))))

    assert len(twenty.proposals) == 20
    assert twenty.search_result_truncated is False
    assert len(excess.proposals) == 20
    assert excess.search_result_truncated is True
    assert all(proposal.search_result_truncated for proposal in excess.proposals)
    assert all(proposal.truncated for proposal in excess.proposals)


def test_memory_sink_is_bounded_and_keeps_accounts_independent() -> None:
    sink = BoundedMemoryProposalSink()
    twenty_one_for_first = _build(
        _search_result(tuple(_post("author", f"post_{index}") for index in range(21)))
    )
    twenty_for_second = _build(
        _search_result(
            tuple(_post("author", f"post_{index}") for index in range(20)),
            account_uuid=_ACCOUNT_TWO,
        ),
        account_uuid=_ACCOUNT_TWO,
    )

    sink.add(twenty_one_for_first)
    sink.add(twenty_for_second)

    assert len(sink.by_account[_ACCOUNT_ONE]) == 20
    assert len(sink.by_account[_ACCOUNT_TWO]) == 20
    assert set(sink.by_account[_ACCOUNT_ONE]) == set(sink.by_account[_ACCOUNT_TWO])
    assert (
        sink.by_account[_ACCOUNT_ONE][_ROOT.replace("root_1", "post_0")]
        != sink.by_account[_ACCOUNT_TWO][_ROOT.replace("root_1", "post_0")]
    )

    extra = replace(
        twenty_one_for_first.proposals[0],
        target_permalink="https://www.threads.com/@author/post/extra_1",
    )
    excess_batch = replace(twenty_one_for_first, proposals=(*twenty_one_for_first.proposals, extra))
    with pytest.raises(ValueError, match="ACTIVE_LIMIT"):
        sink.add(excess_batch)
    assert len(sink.by_account[_ACCOUNT_ONE]) == 20


def test_bridge_deduplicates_canonical_permalink_deterministically() -> None:
    first = _search_item(
        "https://www.threads.com/@Author/post/root_1?utm_source=fixture",
        author="author",
        excerpt="Same evidence",
        position=4,
    )
    duplicate = _search_item(
        "/@author/post/root_1",
        excerpt="Same evidence",
        position=1,
    )

    batch = _build(_manual_search_result((first, duplicate)))

    assert len(batch.proposals) == 1
    assert batch.proposals[0].target_permalink == _ROOT
    assert batch.proposals[0].position == 1


def test_bridge_rejects_conflicting_duplicate_evidence() -> None:
    result = _manual_search_result(
        (
            _search_item(excerpt="First observation", position=0),
            _search_item(excerpt="Contradictory observation", position=1),
        )
    )

    with pytest.raises(BrowserOpportunityBridgeError) as error:
        _build(result)

    assert error.value.code == "BRIDGE_CONTRADICTORY_OBSERVATIONS"


def test_bridge_rejects_conflicting_permalink_author_marker() -> None:
    result = _manual_search_result((_search_item(author="different_author"),))

    with pytest.raises(BrowserOpportunityBridgeError) as error:
        _build(result)

    assert error.value.code == "BRIDGE_CONTRADICTORY_OBSERVATIONS"


def test_partial_and_missing_conversation_context_stay_needs_context() -> None:
    search = _search_result((_post("author", "root_1", text="A synthetic post excerpt"),))
    absent = _build(search).proposals[0]
    incomplete_result = _conversation_model(
        (
            _conversation_item(
                _ROOT,
                "author",
                excerpt="A synthetic post excerpt",
                position=0,
            ),
            _conversation_item(
                _REPLY,
                "reply_author",
                excerpt="Reply without parent evidence",
                position=1,
            ),
        ),
        status="COMPLETE",
    )
    partial = _build(
        search,
        conversations=(
            AccountScopedConversationResult(
                account_uuid=_ACCOUNT_ONE,
                requested_target=_ROOT,
                result=incomplete_result,
            ),
        ),
    ).proposals[0]

    assert absent.context_status == "UNKNOWN"
    assert absent.review_state == "NEEDS_CONTEXT"
    assert partial.context_status == "PARTIAL"
    assert partial.review_state == "NEEDS_CONTEXT"
    assert partial.conversation_observations[1].parent_permalink is None
    assert "CONTEXT_PARTIAL" in partial.reason_codes


def test_nested_parent_evidence_is_preserved_from_synthetic_reader() -> None:
    root = _ROOT
    reply = _REPLY
    nested = "https://www.threads.com/@nested_author/post/nested_1"
    search = _search_result((_post("author", "root_1", text="Root text"),))
    conversation = _conversation_result(
        (
            _post("author", "root_1", text="Root text"),
            _post("reply_author", "reply_1", text="First reply", parent_ref=root),
            _post("nested_author", "nested_1", text="Nested reply", parent_ref=reply),
        )
    )

    proposal = _build(
        search,
        conversations=(AccountScopedConversationResult(_ACCOUNT_ONE, root, conversation),),
    ).proposals[0]

    assert proposal.context_status == "COMPLETE"
    assert [row.target_permalink for row in proposal.conversation_observations] == [
        root,
        reply,
        nested,
    ]
    assert proposal.conversation_observations[1].parent_permalink == root
    assert proposal.conversation_observations[2].parent_permalink == reply
    assert all(
        row.author_identity_status == "UNVERIFIED" for row in proposal.conversation_observations
    )


def test_truncated_excerpt_is_preserved_and_downgrades_context() -> None:
    search = _search_result((_post("author", "root_1", text="A synthetic post excerpt"),))
    complete_but_truncated = _conversation_model(
        (
            _conversation_item(
                _ROOT,
                "author",
                excerpt="A synthetic post excerpt",
                position=0,
                truncated=True,
            ),
        ),
        status="COMPLETE",
    )

    proposal = _build(
        search,
        conversations=(
            AccountScopedConversationResult(_ACCOUNT_ONE, _ROOT, complete_but_truncated),
        ),
    ).proposals[0]

    assert proposal.conversation_observations[0].truncated is True
    assert proposal.truncated is True
    assert proposal.context_status == "PARTIAL"
    assert "EXCERPT_TRUNCATED" in proposal.reason_codes


def test_missing_evidenced_parent_is_not_guessed_from_position() -> None:
    search = _search_result((_post("author", "root_1", text="A synthetic post excerpt"),))
    conversation = _conversation_model(
        (
            _conversation_item(
                _ROOT,
                "author",
                excerpt="A synthetic post excerpt",
                position=0,
            ),
            _conversation_item(
                _REPLY,
                "reply_author",
                excerpt="Reply",
                position=1,
                parent=None,
            ),
        ),
        status="UNKNOWN",
    )

    proposal = _build(
        search,
        conversations=(AccountScopedConversationResult(_ACCOUNT_ONE, _ROOT, conversation),),
    ).proposals[0]

    assert proposal.context_status == "PARTIAL"
    assert proposal.conversation_observations[1].parent_permalink is None


def test_account_and_target_mismatch_fail_closed() -> None:
    search = _search_result((_post("author", "root_1"),))
    with pytest.raises(BrowserOpportunityBridgeError) as account_error:
        build_browser_opportunity_proposals(
            _scoped_search(search), expected_account_uuid=_ACCOUNT_TWO
        )
    assert account_error.value.code == "BRIDGE_ACCOUNT_MISMATCH"

    conversation = _conversation_model(
        (_conversation_item(_ROOT, "author", excerpt="A synthetic post excerpt", position=0),),
        root="https://www.threads.com/@author/post/changed_1",
    )
    with pytest.raises(BrowserOpportunityBridgeError) as target_error:
        _build(
            search,
            conversations=(AccountScopedConversationResult(_ACCOUNT_ONE, _ROOT, conversation),),
        )
    assert target_error.value.code == "BRIDGE_TARGET_MISMATCH"

    with pytest.raises(BrowserOpportunityBridgeError) as scoped_error:
        _build(
            search,
            conversations=(
                AccountScopedConversationResult(_ACCOUNT_TWO, _ROOT, _conversation_model(())),
            ),
        )
    assert scoped_error.value.code == "BRIDGE_ACCOUNT_MISMATCH"


def test_invalid_provenance_fails_closed() -> None:
    manual_item = _search_item(source="MANUAL_URL")
    with pytest.raises(BrowserOpportunityBridgeError) as item_error:
        _build(_manual_search_result((manual_item,)))
    assert item_error.value.code == "BRIDGE_INVALID_PROVENANCE"

    result = _manual_search_result((_search_item(),))
    result.source = cast(_TestSource, "BROWSER_UI")
    with pytest.raises(BrowserOpportunityBridgeError) as result_error:
        _build(result)
    assert result_error.value.code == "BRIDGE_INVALID_PROVENANCE"

    search = _search_result((_post("author", "root_1"),))
    invalid_conversation = _conversation_model(
        (_conversation_item(_ROOT, "author", excerpt=None, position=0),),
    )
    invalid_conversation.source = cast(_TestSource, "MANUAL_URL")
    with pytest.raises(BrowserOpportunityBridgeError) as conversation_error:
        _build(
            search,
            conversations=(
                AccountScopedConversationResult(_ACCOUNT_ONE, _ROOT, invalid_conversation),
            ),
        )
    assert conversation_error.value.code == "BRIDGE_INVALID_PROVENANCE"


def test_twenty_one_v1_items_are_rejected_even_if_model_was_mutated() -> None:
    result = _manual_search_result((_search_item(),))
    observation = _search_item(
        "https://www.threads.com/@author/post/another_1",
        position=1,
    )
    result.observations = (*result.observations, *(observation for _ in range(20)))

    with pytest.raises(BrowserOpportunityBridgeError) as error:
        _build(result)

    assert error.value.code == "BRIDGE_RESULT_LIMIT_EXCEEDED"


def test_bridge_and_synthetic_adapters_have_no_network_browser_or_queue_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected external or process effect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)

    batch = _build(_search_result((_post("author", "root_1"),)))
    sink = BoundedMemoryProposalSink()
    sink.add(batch)

    assert len(sink.by_account[_ACCOUNT_ONE]) == 1
