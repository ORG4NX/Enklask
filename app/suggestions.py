"""Exclusion words suggested from the listings the user hid.

A word that shows up in many hidden titles of a saved search and seldom in
the titles that were kept is a good candidate for its excluded words: hiding
"GameBoy Player" results that are really Wii or GameCube consoles makes
"wii" and "gamecube" come up. Nothing is applied automatically; the topic
settings page offers each word as a one-click addition.
"""

from __future__ import annotations

import math
from collections import Counter

from .filters import normalize

MIN_HIDDEN = 3  # hidden listings needed before suggesting anything
MAX_SUGGESTIONS = 5
MIN_HIDDEN_SHARE = 0.15  # the word is in at least this share of hidden titles
MAX_KEPT_SHARE = 0.10  # ...and in at most this share of the kept ones

# Grammar and condition words: frequent everywhere, never worth excluding.
STOPWORDS = frozenset("""
a au aux avec ce ces d de des du en et for in la le les l of on ou par pour
sans sur the to un une with x
bon bonne tres etat neuf neuve occasion comme parfait parfaite excellent
fonctionne fonctionnel fonctionnelle marche complet complete original
originale officiel officielle vendu vends vend lot pack plus tbe tres
""".split())


def _words(title: str) -> set[str]:
    return {
        w for w in normalize(title).split()
        if len(w) >= 2 and not w.isdigit() and w not in STOPWORDS
    }


def suggest_exclusions(
    hidden_titles: list[str],
    kept_titles: list[str],
    *,
    known: list[str] | None = None,
    limit: int = MAX_SUGGESTIONS,
) -> list[dict]:
    """Words that set the hidden titles apart from the kept ones.

    ``known`` lists words never to suggest: the query, the required and
    already excluded words, and suggestions the user dismissed (phrases are
    split into their words). Each suggestion is ``{"word", "hidden",
    "kept"}``: how many hidden and kept titles contain it, best first.
    """
    if len(hidden_titles) < MIN_HIDDEN:
        return []
    skip: set[str] = set()
    for item in known or []:
        skip.update(normalize(item).split())

    hidden = Counter(w for t in hidden_titles for w in _words(t))
    kept = Counter(w for t in kept_titles for w in _words(t))
    n_hidden, n_kept = len(hidden_titles), len(kept_titles)
    min_hidden = max(2, math.ceil(MIN_HIDDEN_SHARE * n_hidden))
    max_kept = max(1, math.floor(MAX_KEPT_SHARE * n_kept))

    scored = []
    for word, h in hidden.items():
        k = kept.get(word, 0)
        if word in skip or h < min_hidden or k > max_kept:
            continue
        hidden_share = h / n_hidden
        kept_share = k / n_kept if n_kept else 0.0
        # Clearly more common among hidden titles than among kept ones.
        if kept_share and hidden_share < 3 * kept_share:
            continue
        scored.append((hidden_share - kept_share, h, word, k))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return [{"word": w, "hidden": h, "kept": k} for _, h, w, k in scored[:limit]]
