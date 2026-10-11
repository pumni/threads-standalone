from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from threads_platform.browser import read_adapters
from threads_platform.browser.capabilities import (
    BrowserConversationItemResultV1,
    BrowserConversationResultV1,
    BrowserSearchItemResultV1,
    BrowserSearchResultV1,
)
from threads_platform.browser.contracts import (
    BrowserAdapterError,
    BrowserContractError,
    BrowserReadTimeout,
    ChallengeDetected,
    LocatorNotFound,
    UnsupportedUIState,
)
from threads_platform.browser.read_adapters import (
    BROWSER_READ_RESULT_BOUND,
    BROWSER_SEARCH_CANDIDATE_BOUND,
    BROWSER_SEARCH_ITERATION_BOUND,
    SyntheticBrowserConversationAdapter,
    SyntheticBrowserSearchAdapter,
    normalize_conversation_candidates,
    normalize_manual_permalink,
    normalize_search_candidates,
    normalize_search_query,
)


def _candidate(
    post_id: str,
    *,
    author: str = "synthetic",
    excerpt: str | None = None,
) -> dict[str, object]:
    candidate: dict[str, object] = {
        "href": f"/@{author}/post/{post_id}",
        "author_username": author,
    }
    if excerpt is not None:
        candidate["text_excerpt"] = excerpt
    return candidate


class _SyntheticSearchReader:
    def __init__(self, pages: object, *, delay: float = 0) -> None:
        self.pages = pages
        self.delay = delay
        self.calls: list[tuple[str, int, int]] = []
        self.mutation_calls: list[str] = []

    async def search_candidates(
        self,
        query: str,
        *,
        iteration_bound: int,
        candidate_bound: int,
    ) -> Sequence[Sequence[Mapping[str, object]]]:
        self.calls.append((query, iteration_bound, candidate_bound))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.pages  # type: ignore[return-value]


class _SyntheticConversationReader:
    def __init__(self, payload: object, *, delay: float = 0) -> None:
        self.payload = payload
        self.delay = delay
        self.calls: list[tuple[str, int]] = []
        self.mutation_calls: list[str] = []

    async def read_conversation(
        self,
        target_ref: str,
        *,
        candidate_bound: int,
    ) -> tuple[Sequence[Mapping[str, object]], bool | None]:
        self.calls.append((target_ref, candidate_bound))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload  # type: ignore[return-value]


class _FailingSearchReader:
    def __init__(self, error: BrowserAdapterError) -> None:
        self.error = error

    async def search_candidates(
        self,
        query: str,
        *,
        iteration_bound: int,
        candidate_bound: int,
    ) -> Sequence[Sequence[Mapping[str, object]]]:
        raise self.error


def test_read_models_are_versioned_bounded_and_reject_extra_fields() -> None:
    search_item = BrowserSearchItemResultV1(
        thread_ref="https://www.threads.com/@synthetic/post/fake_1",
        author_username="synthetic",
        text_excerpt=None,
        position=0,
        source="MANUAL_URL",
        context_status="UNKNOWN",
        truncated=False,
    )
    search_result = BrowserSearchResultV1(
        query="hiring",
        observations=(search_item,),
        truncated=False,
        source="SYNTHETIC_FIXTURE",
    )
    conversation_item = BrowserConversationItemResultV1(
        thread_ref=search_item.thread_ref,
        author_username="synthetic",
        position=0,
        truncated=False,
    )
    conversation_result = BrowserConversationResultV1(
        root_thread_ref=search_item.thread_ref,
        observations=(conversation_item,),
        context_status="UNKNOWN",
        truncated=False,
        source="SYNTHETIC_FIXTURE",
    )

    assert search_result.result_version == conversation_result.result_version == 1
    assert search_item.source == "MANUAL_URL"
    assert conversation_result.observations == (conversation_item,)
    with pytest.raises(ValidationError):
        BrowserSearchResultV1(
            query="hiring",
            observations=tuple(search_item for _ in range(21)),
            truncated=True,
            source="SYNTHETIC_FIXTURE",
        )
    with pytest.raises(ValidationError):
        BrowserSearchItemResultV1(
            thread_ref="/bad",
            author_username="synthetic",
            position=0,
            source="MANUAL_URL",
            context_status="UNKNOWN",
            truncated=False,
            unexpected="value",
        )
    with pytest.raises(ValidationError):
        BrowserConversationItemResultV1(
            thread_ref="/bad",
            author_username="synthetic",
            position=20,
            truncated=False,
        )


def test_pure_read_normalizers_are_offline_and_do_not_write(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())
    result = normalize_manual_permalink(
        "HTTPS://WWW.THREADS.COM/@Synthetic/post/fake_1?utm_source=operator&xmt=share#comments"
    )
    search = normalize_search_candidates(
        "role",
        (({"href": "/@synthetic/post/fake_2", "author_username": "synthetic"},),),
    )
    conversation = normalize_conversation_candidates(
        result.thread_ref,
        (
            {
                "href": result.thread_ref,
                "author_username": "synthetic",
                "parent_ref": None,
            },
        ),
        context_complete=True,
    )

    assert result.thread_ref == "https://www.threads.com/@synthetic/post/fake_1"
    assert result.author_username == "synthetic"
    assert result.source == "MANUAL_URL"
    assert result.context_status == "UNKNOWN"
    assert result.text_excerpt is None
    assert len(search.observations) == 1
    assert conversation.context_status == "COMPLETE"
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "value",
    [
        "//evil.invalid/@synthetic/post/fake_1",
        "https://evil.invalid/@synthetic/post/fake_1",
        "https://www.threads.com.evil.invalid/@synthetic/post/fake_1",
        "https://user@www.threads.com/@synthetic/post/fake_1",
        "https://www.threads.com:443/@synthetic/post/fake_1",
        "https://www.threads.com/%40synthetic/post/fake_1",
        "https://www.threads.com/@synthetic/post/%66ake_1",
        "https://www.threads.com/@synthetic/post/___",
        "https://www.threads.com/@synthetic/post/fake_1?next=https%3A%2F%2Fevil.invalid",
        "https://www.threads.com/@synthetic/post/fake_1?xmt=one&xmt=two",
        "https://www.threads.com/@synthetic/post/fake_1?xmt=bad%zz",
        "https://www.threads.com/@synthetic/post/fake_1#javascript:alert(1)",
        "/@synthetic/post/fake_1\\@evil.invalid",
    ],
)
def test_manual_permalink_rejects_untrusted_origins_and_url_tricks(value: str) -> None:
    with pytest.raises(BrowserContractError):
        normalize_manual_permalink(value)


@pytest.mark.parametrize(
    "query",
    [None, "", " \t ", "x" * 121, "two\nlines", "a\x00b", "a\u2028b"],
)
def test_search_query_requires_explicit_bounded_single_line_text(query: object) -> None:
    with pytest.raises(BrowserContractError):
        normalize_search_query(query)


def test_search_query_trims_operator_whitespace() -> None:
    assert normalize_search_query("  synthetic hiring  ") == "synthetic hiring"


@pytest.mark.parametrize("count", [0, 1, 20, 21])
def test_synthetic_search_bounds_results_and_marks_truncation(count: int) -> None:
    candidates = tuple(_candidate(f"fake_{index}") for index in range(count))
    result = normalize_search_candidates(" hiring ", (candidates,))

    expected = min(count, BROWSER_READ_RESULT_BOUND)
    assert result.query == "hiring"
    assert len(result.observations) == expected
    assert [item.position for item in result.observations] == list(range(expected))
    assert result.truncated is (count > BROWSER_READ_RESULT_BOUND)
    assert result.source == "SYNTHETIC_FIXTURE"
    assert all(item.context_status == "UNKNOWN" for item in result.observations)


def test_search_deduplicates_canonical_permalink_and_caps_five_pages() -> None:
    pages = (
        (
            {
                "href": "https://www.threads.com/@Synthetic/post/fake_1?xmt=safe",
                "author_username": "Synthetic",
            },
        ),
        ({"href": "/@synthetic/post/fake_1/", "author_username": "synthetic"},),
        ({"href": "/@synthetic/post/fake_2", "author_username": "synthetic"},),
        (),
        (),
    )
    reader = _SyntheticSearchReader(pages)
    result = asyncio.run(SyntheticBrowserSearchAdapter(reader).search("  design role  "))

    assert len(result.observations) == 2
    assert [item.position for item in result.observations] == [0, 1]
    assert reader.calls == [
        ("design role", BROWSER_SEARCH_ITERATION_BOUND, BROWSER_SEARCH_CANDIDATE_BOUND)
    ]
    assert reader.mutation_calls == []


def test_synthetic_search_candidate_flows_to_exact_bounded_conversation() -> None:
    search_reader = _SyntheticSearchReader(
        (({"href": "/@synthetic/post/selected_1", "author_username": "synthetic"},),)
    )
    conversation_reader = _SyntheticConversationReader(
        (
            (
                {
                    "href": "/@synthetic/post/selected_1",
                    "author_username": "synthetic",
                    "text_excerpt": "bounded root excerpt",
                    "parent_ref": None,
                },
                {
                    "href": "/@reply_author/post/reply_1",
                    "author_username": "reply_author",
                    "text_excerpt": "bounded reply excerpt",
                    "parent_ref": "/@synthetic/post/selected_1",
                },
            ),
            True,
        )
    )

    async def run_fixture_flow() -> tuple[BrowserSearchResultV1, BrowserConversationResultV1]:
        search_result = await SyntheticBrowserSearchAdapter(search_reader).search("design role")
        exact_target = search_result.observations[0].thread_ref
        conversation = await SyntheticBrowserConversationAdapter(conversation_reader).read(
            exact_target
        )
        return search_result, conversation

    search_result, conversation_result = asyncio.run(run_fixture_flow())

    assert conversation_reader.calls[0][0] == search_result.observations[0].thread_ref
    assert conversation_result.root_thread_ref == search_result.observations[0].thread_ref
    assert conversation_result.context_status == "COMPLETE"
    assert [item.parent_ref for item in conversation_result.observations] == [
        None,
        conversation_result.root_thread_ref,
    ]
    assert search_reader.mutation_calls == conversation_reader.mutation_calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        {"href": "//evil.invalid/@synthetic/post/fake_1", "author_username": "synthetic"},
        {"href": "https://evil.invalid/@synthetic/post/fake_1", "author_username": "synthetic"},
        {"href": "/@synthetic/post/fake_1", "author_username": "someone_else"},
        {"href": "/@synthetic/post/fake_1?next=evil", "author_username": "synthetic"},
        {"href": "/@synthetic/post/fake_1", "author_username": "synthetic", "raw_dom": "private"},
    ],
)
def test_search_stops_on_wrong_origin_author_or_unbounded_payload(
    candidate: dict[str, object],
) -> None:
    with pytest.raises(UnsupportedUIState):
        normalize_search_candidates("role", ((candidate,),))


def test_search_rejects_more_than_five_iterations_or_one_hundred_candidates() -> None:
    with pytest.raises(UnsupportedUIState):
        normalize_search_candidates("role", tuple(() for _ in range(6)))
    with pytest.raises(UnsupportedUIState):
        normalize_search_candidates(
            "role",
            (tuple(_candidate(f"fake_{index}") for index in range(101)),),
        )


def test_search_adapter_maps_read_timeout_to_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_adapters, "BROWSER_READ_TIMEOUT_SECONDS", 0.001)
    reader = _SyntheticSearchReader((), delay=0.05)

    with pytest.raises(BrowserReadTimeout):
        asyncio.run(SyntheticBrowserSearchAdapter(reader).search("role"))


@pytest.mark.parametrize("error", [ChallengeDetected(), LocatorNotFound()])
def test_search_adapter_stops_on_challenge_or_missing_fixture_selector(
    error: BrowserAdapterError,
) -> None:
    with pytest.raises(type(error)):
        asyncio.run(SyntheticBrowserSearchAdapter(_FailingSearchReader(error)).search("role"))


def test_conversation_preserves_exact_root_and_explicit_nested_parent_relations() -> None:
    root = "https://www.threads.com/@synthetic/post/root_1"
    candidates = (
        {
            "href": root,
            "author_username": "synthetic",
            "text_excerpt": "root post",
            "parent_ref": None,
        },
        {
            "href": "/@reply_author/post/reply_1",
            "author_username": "reply_author",
            "text_excerpt": "first reply",
            "parent_ref": root,
        },
        {
            "href": "/@nested_author/post/reply_2",
            "author_username": "nested_author",
            "text_excerpt": "nested reply",
            "parent_ref": "/@reply_author/post/reply_1",
        },
    )
    result = normalize_conversation_candidates(root, candidates, context_complete=True)

    assert result.root_thread_ref == root
    assert result.context_status == "COMPLETE"
    assert [item.position for item in result.observations] == [0, 1, 2]
    assert [item.parent_ref for item in result.observations] == [
        None,
        root,
        "https://www.threads.com/@reply_author/post/reply_1",
    ]


def test_conversation_marks_missing_and_unknown_parent_as_partial() -> None:
    root = "/@synthetic/post/root_1"
    result = normalize_conversation_candidates(
        root,
        (
            {"href": root, "author_username": "synthetic", "parent_ref": None},
            {
                "href": "/@reply_author/post/reply_1",
                "author_username": "reply_author",
            },
            {
                "href": "/@reply_author/post/reply_2",
                "author_username": "reply_author",
                "parent_ref": "/@missing_author/post/missing_1",
            },
        ),
        context_complete=True,
    )

    assert result.context_status == "PARTIAL"
    assert result.observations[1].parent_ref is None
    assert result.observations[2].parent_ref is None


@pytest.mark.parametrize("context_complete,expected", [(False, "PARTIAL"), (None, "UNKNOWN")])
def test_conversation_status_does_not_overstate_incomplete_fixture(
    context_complete: bool | None,
    expected: str,
) -> None:
    root = "/@synthetic/post/root_1"
    result = normalize_conversation_candidates(
        root,
        ({"href": root, "author_username": "synthetic", "parent_ref": None},),
        context_complete=context_complete,
    )
    assert result.context_status == expected


@pytest.mark.parametrize(
    "candidates",
    [
        (
            {
                "href": "/@someone_else/post/root_1",
                "author_username": "someone_else",
                "parent_ref": None,
            },
        ),
        (
            {
                "href": "/@synthetic/post/root_1",
                "author_username": "wrong_author",
                "parent_ref": None,
            },
        ),
        (
            {"href": "/@synthetic/post/root_1", "author_username": "synthetic", "parent_ref": None},
            {
                "href": "/@reply_author/post/reply_1",
                "author_username": "reply_author",
                "parent_ref": "//evil.invalid/@synthetic/post/root_1",
            },
        ),
    ],
)
def test_conversation_stops_on_off_target_mismatched_author_or_bad_parent(
    candidates: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(UnsupportedUIState):
        normalize_conversation_candidates(
            "/@synthetic/post/root_1",
            candidates,
            context_complete=True,
        )


def test_conversation_clips_excerpt_and_caps_structured_result() -> None:
    root = "/@synthetic/post/root_1"
    candidates: list[dict[str, object]] = [
        {"href": root, "author_username": "synthetic", "parent_ref": None}
    ]
    candidates.extend(
        {
            "href": f"/@reply_author/post/reply_{index}",
            "author_username": "reply_author",
            "text_excerpt": "x" * 700 if index == 0 else None,
            "parent_ref": root,
        }
        for index in range(20)
    )
    result = normalize_conversation_candidates(root, candidates, context_complete=True)

    assert len(result.observations) == BROWSER_READ_RESULT_BOUND
    assert result.truncated is True
    assert result.context_status == "PARTIAL"
    assert len(result.observations[1].text_excerpt or "") == 500
    assert result.observations[1].truncated is True


def test_conversation_requires_root_and_rejects_parent_cycles_and_absurd_payloads() -> None:
    root = "/@synthetic/post/root_1"
    with pytest.raises(UnsupportedUIState):
        normalize_conversation_candidates(
            root,
            ({"href": "/@other/post/other_1", "author_username": "other", "parent_ref": None},),
            context_complete=True,
        )
    with pytest.raises(UnsupportedUIState):
        normalize_conversation_candidates(
            root,
            (
                {"href": root, "author_username": "synthetic", "parent_ref": None},
                {
                    "href": "/@reply_author/post/reply_1",
                    "author_username": "reply_author",
                    "parent_ref": "/@reply_author/post/reply_1",
                },
            ),
            context_complete=True,
        )
    with pytest.raises(UnsupportedUIState):
        normalize_conversation_candidates(
            root,
            tuple(
                {
                    "href": f"/@reply_author/post/reply_{index}",
                    "author_username": "reply_author",
                    "parent_ref": root,
                }
                for index in range(101)
            ),
            context_complete=True,
        )


def test_conversation_adapter_normalizes_target_and_passes_finite_candidate_bound() -> None:
    root = "/@synthetic/post/root_1"
    reader = _SyntheticConversationReader(
        (({"href": root, "author_username": "synthetic", "parent_ref": None},), True)
    )
    result = asyncio.run(SyntheticBrowserConversationAdapter(reader).read(root + "?xmt=safe"))

    assert reader.calls == [
        ("https://www.threads.com/@synthetic/post/root_1", BROWSER_SEARCH_CANDIDATE_BOUND)
    ]
    assert result.root_thread_ref == "https://www.threads.com/@synthetic/post/root_1"
    assert reader.mutation_calls == []


def test_conversation_adapter_timeout_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_adapters, "BROWSER_READ_TIMEOUT_SECONDS", 0.001)
    reader = _SyntheticConversationReader(((), False), delay=0.05)

    with pytest.raises(BrowserReadTimeout):
        asyncio.run(SyntheticBrowserConversationAdapter(reader).read("/@synthetic/post/root_1"))
