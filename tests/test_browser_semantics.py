from __future__ import annotations

import pytest

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.semantics import (
    normalize_feed_permalinks,
    normalize_profile_ref,
    normalize_profile_username,
    parse_thread_ref,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("synthetic.user", "/@synthetic.user"),
        ("synthetic_user", "/@synthetic_user"),
        ("@synthetic", None),
        ("synthetic user", None),
        ("synthetic/post/1", None),
    ],
)
def test_profile_username_normalization_is_narrow(value: object, expected: str | None) -> None:
    assert normalize_profile_username(value) == expected


def test_profile_reference_requires_one_safe_local_path() -> None:
    assert normalize_profile_ref("/@Synthetic.User/") == "/@Synthetic.User"
    for value in (
        "https://www.threads.com/@synthetic",
        "//evil.invalid/@synthetic",
        "/@synthetic?next=other",
        "/@synthetic/post/fake_1",
        "/@synthetic/../settings",
    ):
        assert normalize_profile_ref(value) is None


def test_thread_reference_only_accepts_safe_profile_post_shapes() -> None:
    assert parse_thread_ref("/@Synthetic/post/fake_1") == (
        "/@Synthetic/post/fake_1",
        "Synthetic",
    )
    for value in (
        "https://www.threads.com/@synthetic/post/fake_1",
        "//evil.invalid/@synthetic/post/fake_1",
        "/@synthetic/post/fake_1?next=other",
        "/@synthetic/post/../settings",
        "/@synthetic/post/fake_1/extra",
    ):
        assert parse_thread_ref(value) is None


def test_feed_normalization_filters_origins_decorations_and_duplicates() -> None:
    result = normalize_feed_permalinks(
        (
            "https://www.threads.com/@Synthetic/post/fake_1?x=ignored#fragment",
            "/@synthetic/post/fake_2/",
            "https://evil.invalid/@synthetic/post/fake_3",
            "//evil.invalid/@synthetic/post/fake_4",
            "javascript:alert(1)",
            "https://www.threads.com/@synthetic/post/fake_1",
        ),
        max_items=3,
    )

    assert [item.thread_ref for item in result] == [
        "https://www.threads.com/@synthetic/post/fake_1",
        "https://www.threads.com/@synthetic/post/fake_2",
    ]
    assert [item.position for item in result] == [0, 1]
    assert all(item.text_excerpt is None for item in result)


def test_feed_normalization_rejects_unbounded_candidates() -> None:
    with pytest.raises(BrowserContractError):
        normalize_feed_permalinks(tuple("/invalid" for _ in range(101)), max_items=2)
