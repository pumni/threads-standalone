"""Bounded, synthetic-only browser discovery and conversation read adapters."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import TypeGuard, cast
from unicodedata import category
from urllib.parse import parse_qsl, urlsplit

from threads_platform.browser.capabilities import (
    BrowserConversationItemResultV1,
    BrowserConversationResultV1,
    BrowserSearchItemResultV1,
    BrowserSearchResultV1,
)
from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserContractError,
    BrowserConversationEngineSession,
    BrowserReadTimeout,
    BrowserSearchEngineSession,
    UnsupportedUIState,
)
from threads_platform.browser.semantics import parse_thread_ref

BROWSER_SEARCH_ITERATION_BOUND = 5
BROWSER_SEARCH_CANDIDATE_BOUND = 100
BROWSER_READ_RESULT_BOUND = 20
BROWSER_READ_EXCERPT_BOUND = 500
BROWSER_READ_INPUT_EXCERPT_BOUND = 1000
BROWSER_READ_TIMEOUT_SECONDS = 30
BROWSER_SEARCH_QUERY_BOUND = 120
_THREAD_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_SAFE_THREAD_FRAGMENT = re.compile(r"(?:comments|replies|post|comment-[A-Za-z0-9_-]{1,120})")
_SAFE_TRACKING_QUERY_KEYS = frozenset(
    {
        "igsh",
        "share",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
        "xmt",
    }
)
_SEARCH_CANDIDATE_KEYS = frozenset({"href", "author_username", "text_excerpt"})
_CONVERSATION_CANDIDATE_KEYS = frozenset({"href", "author_username", "text_excerpt", "parent_ref"})


def normalize_search_query(query: object) -> str:
    if not isinstance(query, str):
        raise BrowserContractError()
    normalized = query.strip()
    if not 1 <= len(normalized) <= BROWSER_SEARCH_QUERY_BOUND or any(
        category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in normalized
    ):
        raise BrowserContractError()
    return normalized


def normalize_manual_permalink(value: object) -> BrowserSearchItemResultV1:
    """Normalize an operator-provided permalink without opening a browser."""

    target = _canonical_thread_ref(value)
    if target is None:
        raise BrowserContractError()
    thread_ref, author_username = target
    return BrowserSearchItemResultV1(
        thread_ref=thread_ref,
        author_username=author_username,
        text_excerpt=None,
        position=0,
        source="MANUAL_URL",
        context_status="UNKNOWN",
        truncated=False,
    )


def normalize_search_candidates(
    query: object,
    pages: object,
    *,
    max_items: int = BROWSER_READ_RESULT_BOUND,
) -> BrowserSearchResultV1:
    normalized_query = normalize_search_query(query)
    _validate_result_bound(max_items)
    if not _is_sequence(pages) or len(pages) > BROWSER_SEARCH_ITERATION_BOUND:
        raise UnsupportedUIState()

    candidates: dict[str, tuple[str, str | None, bool]] = {}
    candidate_count = 0
    for page in pages:
        if not _is_sequence(page):
            raise UnsupportedUIState()
        candidate_count += len(page)
        if candidate_count > BROWSER_SEARCH_CANDIDATE_BOUND:
            raise UnsupportedUIState()
        for candidate in page:
            reference, author, excerpt, excerpt_truncated = _normalize_candidate(
                candidate,
                allowed_keys=_SEARCH_CANDIDATE_KEYS,
            )
            previous = candidates.get(reference)
            normalized = (author, excerpt, excerpt_truncated)
            if previous is not None and previous != normalized:
                raise UnsupportedUIState()
            candidates.setdefault(reference, normalized)

    truncated = len(candidates) > max_items
    selected = list(candidates.items())[:max_items]
    observations = tuple(
        BrowserSearchItemResultV1(
            thread_ref=reference,
            author_username=details[0],
            text_excerpt=details[1],
            position=position,
            source="SYNTHETIC_FIXTURE",
            context_status="UNKNOWN",
            truncated=details[2],
        )
        for position, (reference, details) in enumerate(selected)
    )
    truncated = truncated or any(item.truncated for item in observations)
    return BrowserSearchResultV1(
        query=normalized_query,
        observations=observations,
        truncated=truncated,
        source="SYNTHETIC_FIXTURE",
    )


def normalize_conversation_candidates(
    target_ref: object,
    candidates: object,
    *,
    context_complete: bool | None,
    max_items: int = BROWSER_READ_RESULT_BOUND,
) -> BrowserConversationResultV1:
    root = _canonical_thread_ref(target_ref)
    if root is None:
        raise BrowserContractError()
    _validate_result_bound(max_items)
    if (
        not _is_sequence(candidates)
        or len(candidates) > BROWSER_SEARCH_CANDIDATE_BOUND
        or (context_complete is not None and type(context_complete) is not bool)
    ):
        raise UnsupportedUIState()
    root_ref = root[0]

    records: dict[str, tuple[str, str | None, bool, str | None]] = {}
    for candidate in candidates:
        reference, author, excerpt, excerpt_truncated = _normalize_candidate(
            candidate,
            allowed_keys=_CONVERSATION_CANDIDATE_KEYS,
        )
        if not _is_candidate_mapping(candidate):
            raise UnsupportedUIState()
        parent_value = candidate.get("parent_ref")
        if parent_value is not None:
            parent = _canonical_thread_ref(parent_value)
            if parent is None:
                raise UnsupportedUIState()
            parent_ref = parent[0]
        else:
            parent_ref = None
        if reference == root_ref and parent_ref is not None:
            raise UnsupportedUIState()

        normalized = (author, excerpt, excerpt_truncated, parent_ref)
        previous = records.get(reference)
        if previous is not None and previous != normalized:
            raise UnsupportedUIState()
        records.setdefault(reference, normalized)

    if root_ref not in records:
        raise UnsupportedUIState()

    selected_refs = [root_ref]
    selected_refs.extend(reference for reference in records if reference != root_ref)
    truncated = len(selected_refs) > max_items
    selected_refs = selected_refs[:max_items]
    selected_set = set(selected_refs)
    missing_parent = False
    result_items: list[BrowserConversationItemResultV1] = []
    selected_parents: dict[str, str | None] = {}

    for position, reference in enumerate(selected_refs):
        author, excerpt, excerpt_truncated, parent_ref = records[reference]
        if reference == root_ref:
            parent_ref = None
        elif parent_ref is None or parent_ref not in selected_set:
            parent_ref = None
            missing_parent = True
        selected_parents[reference] = parent_ref
        truncated = truncated or excerpt_truncated
        result_items.append(
            BrowserConversationItemResultV1(
                thread_ref=reference,
                author_username=author,
                text_excerpt=excerpt,
                parent_ref=parent_ref,
                position=position,
                truncated=excerpt_truncated,
            )
        )

    _reject_parent_cycles(selected_parents)
    if context_complete is False or missing_parent or truncated:
        context_status = "PARTIAL"
    elif context_complete is None:
        context_status = "UNKNOWN"
    else:
        context_status = "COMPLETE"
    return BrowserConversationResultV1(
        root_thread_ref=root_ref,
        observations=tuple(result_items),
        context_status=context_status,
        truncated=truncated,
        source="SYNTHETIC_FIXTURE",
    )


class SyntheticBrowserSearchAdapter:
    """Runs bounded search parsing against an explicitly injected synthetic reader."""

    def __init__(self, session: BrowserSearchEngineSession) -> None:
        self._session = session

    async def search(
        self,
        query: object,
        *,
        max_items: int = BROWSER_READ_RESULT_BOUND,
    ) -> BrowserSearchResultV1:
        normalized_query = normalize_search_query(query)
        _validate_result_bound(max_items)
        try:
            async with asyncio.timeout(BROWSER_READ_TIMEOUT_SECONDS):
                pages = await self._session.search_candidates(
                    normalized_query,
                    iteration_bound=BROWSER_SEARCH_ITERATION_BOUND,
                    candidate_bound=BROWSER_SEARCH_CANDIDATE_BOUND,
                )
        except TimeoutError:
            raise BrowserReadTimeout() from None
        return normalize_search_candidates(
            normalized_query,
            pages,
            max_items=max_items,
        )


class SyntheticBrowserConversationAdapter:
    """Reads an exact target from an explicitly injected synthetic reader."""

    def __init__(self, session: BrowserConversationEngineSession) -> None:
        self._session = session

    async def read(
        self,
        target_ref: object,
        *,
        max_items: int = BROWSER_READ_RESULT_BOUND,
    ) -> BrowserConversationResultV1:
        root = _canonical_thread_ref(target_ref)
        if root is None:
            raise BrowserContractError()
        _validate_result_bound(max_items)
        try:
            async with asyncio.timeout(BROWSER_READ_TIMEOUT_SECONDS):
                payload = await self._session.read_conversation(
                    root[0],
                    candidate_bound=BROWSER_SEARCH_CANDIDATE_BOUND,
                )
        except TimeoutError:
            raise BrowserReadTimeout() from None
        if type(payload) is not tuple or len(payload) != 2:
            raise UnsupportedUIState()
        candidates, context_complete = payload
        return normalize_conversation_candidates(
            root[0],
            candidates,
            context_complete=context_complete,
            max_items=max_items,
        )


def _validate_result_bound(max_items: int) -> None:
    if type(max_items) is not int or not 1 <= max_items <= BROWSER_READ_RESULT_BOUND:
        raise BrowserContractError()


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_candidate_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return False
    candidate = cast(Mapping[object, object], value)
    return all(isinstance(key, str) for key in candidate)


def _normalize_candidate(
    candidate: object,
    *,
    allowed_keys: frozenset[str],
) -> tuple[str, str, str | None, bool]:
    if not _is_candidate_mapping(candidate):
        raise UnsupportedUIState()
    if len(candidate) > len(allowed_keys):
        raise UnsupportedUIState()
    keys = set(candidate)
    required = {"href", "author_username"}
    if not required.issubset(keys) or not keys.issubset(allowed_keys):
        raise UnsupportedUIState()
    target = _canonical_thread_ref(candidate.get("href"))
    author = candidate.get("author_username")
    if (
        target is None
        or not isinstance(author, str)
        or _THREAD_USERNAME.fullmatch(author) is None
        or author.casefold() != target[1]
    ):
        raise UnsupportedUIState()
    excerpt, excerpt_truncated = _normalize_excerpt(candidate.get("text_excerpt"))
    return target[0], target[1], excerpt, excerpt_truncated


def _normalize_excerpt(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, str) or len(value) > BROWSER_READ_INPUT_EXCERPT_BOUND:
        raise UnsupportedUIState()
    normalized = " ".join(value.split())
    if not normalized:
        return None, False
    return normalized[:BROWSER_READ_EXCERPT_BOUND], len(normalized) > BROWSER_READ_EXCERPT_BOUND


def _canonical_thread_ref(value: object) -> tuple[str, str] | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() != "www.threads.com":
            return None
    elif parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    if "%" in parsed.path or "\\" in parsed.path:
        return None
    if parsed.query and not _safe_tracking_query(parsed.query):
        return None
    if parsed.fragment and _SAFE_THREAD_FRAGMENT.fullmatch(parsed.fragment) is None:
        return None
    parsed_ref = parse_thread_ref(parsed.path)
    if parsed_ref is None:
        return None
    thread_ref, author = parsed_ref
    post_id = thread_ref.rsplit("/", 1)[-1]
    if not any(char.isalnum() for char in post_id):
        return None
    normalized_author = author.casefold()
    return f"{BROWSER_FEED_ORIGIN}/@{normalized_author}/post/{post_id}", normalized_author


def _safe_tracking_query(query: str) -> bool:
    if len(query) > 1024 or ";" in query or re.search(r"%(?![0-9a-fA-F]{2})", query):
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except ValueError:
        return False
    seen: set[str] = set()
    for key, value in pairs:
        normalized_key = key.casefold()
        if (
            normalized_key not in _SAFE_TRACKING_QUERY_KEYS
            or normalized_key in seen
            or len(value) > 256
            or any(category(char) == "Cc" for char in value)
        ):
            return False
        seen.add(normalized_key)
    return bool(pairs)


def _reject_parent_cycles(parents: Mapping[str, str | None]) -> None:
    for reference in parents:
        seen: set[str] = set()
        current: str | None = reference
        while current is not None and current in parents:
            if current in seen:
                raise UnsupportedUIState()
            seen.add(current)
            current = parents[current]
