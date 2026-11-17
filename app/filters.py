"""Per-search result filters applied before anything is stored or notified."""

from __future__ import annotations

import re
import unicodedata

MAX_EXCLUDE_WORDS = 30
MAX_EXCLUDE_WORD_LENGTH = 64

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """Lowercase, accents stripped, punctuation folded to single spaces.

    "Écran  Wii-U!" -> "ecran wii u"
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", ascii_only.lower()).strip()


def parse_exclude_words(raw: str) -> list[str]:
    """Comma-separated words or phrases, trimmed, de-duplicated, capped."""
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        word = part.strip()[:MAX_EXCLUDE_WORD_LENGTH]
        key = normalize(word)
        if key and key not in seen:
            seen.add(key)
            out.append(word)
    return out[:MAX_EXCLUDE_WORDS]


def excluded_by(title: str, words: list[str] | None) -> str | None:
    """The first excluded word found in ``title`` as a whole word, else None.

    Matching ignores case, accents and punctuation, and works on whole words:
    "game cube" matches "Game-Cube" but not "GameCube", and "wii" does not
    match "WiiU". List each spelling that should be excluded.
    """
    if not words:
        return None
    haystack = f" {normalize(title)} "
    for word in words:
        needle = normalize(word)
        if needle and f" {needle} " in haystack:
            return word
    return None


def drop_excluded(raws: list, words: list[str] | None) -> list:
    """``raws`` without the listings whose title contains an excluded word."""
    if not words:
        return list(raws)
    return [raw for raw in raws if excluded_by(raw.title, words) is None]


def drop_out_of_price(
    raws: list, price_min: float | None, price_max: float | None
) -> list:
    """``raws`` without the listings priced outside ``[price_min, price_max]``.

    Safety net for sources that silently drop the price filter (Easy Cash
    redirects many searches to a category page without it). Listings with an
    unknown price are kept: there is nothing to judge them on.
    """
    if price_min is None and price_max is None:
        return list(raws)
    return [
        raw for raw in raws
        if raw.price is None
        or ((price_min is None or raw.price >= price_min)
            and (price_max is None or raw.price <= price_max))
    ]
