"""Easy Cash source adapter (unofficial, server-rendered search page).

Easy Cash is a French chain of second-hand shops; its online catalogue
(``bons-plans.easycash.fr``) is strong on consoles and retro games. The search
page is rendered server-side, and every result card carries the analytics
payload the site itself uses (``data-layer-click``: id, name, price), which is
a steadier contract than the surrounding markup.

One result is a *catalogue product* that may group several physical offers
("à partir de 129,99 €"), not a single ad: the price is the cheapest offer and
its changes feed the price history like any other listing. Condition cannot
be filtered here (everything is second-hand, graded per offer), so
``query.condition`` is ignored. Best-effort and isolated like the other
unofficial sources.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape

import httpx

from ..config import get_settings
from ..enums import Source
from ..filters import drop_off_query
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
)
# Our generic sort -> the site's ``sort`` parameter (absent = relevance).
# "recent" is left on relevance: the site's search is fuzzy, and sorted by
# date its first page is mostly loose matches (books, printers for "PSOne").
_SORTS = {"price_asc": "asc", "price_desc": "desc"}

_CARD_RE = re.compile(
    r'<li\s+class="clearfix block-link"\s+data-href="(?P<url>[^"]+)"\s+'
    r'data-layer-click="(?P<data>[^"]+)"',
)
# Thumbnails go through an image resizer; the original sits on the CDN.
_IMAGE_RE = re.compile(r"https://cdn\.easycash\.fr/img/prod/[0-9/]+/src/[^\s?\"'<>]+")


class EasyCashSource(BaseSource):
    name = Source.EASYCASH

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.easycash_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_easycash)

    def search(self, query: SearchQuery) -> list[RawListing]:
        params: dict[str, str] = {"q": query.query}
        if query.price_min is not None:
            params["minPrice"] = f"{query.price_min:g}"
        if query.price_max is not None:
            params["maxPrice"] = f"{query.price_max:g}"
        sort = _SORTS.get(query.sort)
        if sort:
            params["sort"] = sort
        resp = httpx.get(
            f"{self._base}/catalog/search",
            params=params,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        # The site pads its results with loose matches: keep the titles that
        # really contain every word searched.
        return drop_off_query(parse_search_page(resp.text), query.query)


def parse_search_page(html: str) -> list[RawListing]:
    """Result cards of a search page, in page order (duplicates dropped)."""
    raws: list[RawListing] = []
    seen: set[str] = set()
    cards = list(_CARD_RE.finditer(html))
    for index, card in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else len(html)
        raw = _to_raw(card, html[card.end():end])
        if raw is not None and raw.source_id not in seen:
            seen.add(raw.source_id)
            raws.append(raw)
    return raws


def _to_raw(card: re.Match, body: str) -> RawListing | None:
    try:
        item = json.loads(unescape(card["data"]))["ecommerce"]["items"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    item_id = str(item.get("item_id") or "").strip()
    title = str(item.get("item_name") or "").strip()
    if not item_id or not title:
        return None
    try:
        price = float(item["price"])
    except (KeyError, TypeError, ValueError):
        price = None
    image = _IMAGE_RE.search(unescape(body))
    thumb = image.group(0) if image else None
    return RawListing(
        source=Source.EASYCASH,
        source_id=item_id,
        title=title,
        url=unescape(card["url"]),
        price=price,
        currency="EUR",
        thumbnail=thumb,
        photos=[thumb] if thumb else [],
    )
