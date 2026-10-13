"""Pure text matching shared by independent Nurture source lanes."""

from __future__ import annotations

import unicodedata

MAX_NURTURE_TEXT_CODEPOINTS = 10_000


def normalize_nurture_text(text: str | None) -> str | None:
    """Bound and normalize text using the existing Nurture policy semantics."""

    if text is None:
        return None
    bounded_text = text[:MAX_NURTURE_TEXT_CODEPOINTS]
    try:
        return unicodedata.normalize("NFKC", bounded_text).casefold()
    except UnicodeError:
        return None


def matches_nurture_terms(normalized_text: str, terms: tuple[str, ...]) -> bool:
    """Return whether any normalized term occurs in already normalized text."""

    return any(
        normalized_term in normalized_text
        for term in terms
        if (normalized_term := normalize_nurture_text(term)) is not None and normalized_term
    )
