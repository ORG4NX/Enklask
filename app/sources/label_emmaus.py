"""Label Emmaüs source adapter (unofficial, server-rendered catalogue).

Label Emmaüs (``label-emmaus.co``) is the online marketplace of the Emmaüs
movement and other social-economy sellers: every result is a single
second-hand item sold by one shop. The catalogue page is rendered server-side
and each card carries the analytics payload the site itself uses
(``data-ga4-item``: id, price, seller), which is steadier than the markup
around it. Price, condition and sort are filtered by the site itself.
Best-effort and isolated like the other unofficial sources.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from urllib.parse import urljoin

import httpx

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
)
# Our generic sort -> the site's ``tri`` parameter (absent = relevance).
_SORTS = {"recent": "recent", "price_asc": "price_asc", "price_desc": "price_desc"}
# Our generic condition bucket -> the site's ``etat`` facet. "for_parts" has no
# equivalent (the site only sells working items), so it is left unfiltered.
_CONDITIONS = {
    "new": "neuf",
    "like_new": "très bon état",
    "good": "bon état",
    "fair": "état correct",
}

_CARD_RE = re.compile(
    r'<div class="product-card catalogue-card"\s+data-id="(?P<id>\d+)"\s+'
    r'data-ga4-item="(?P<data>[^"]+)"',
)
_LINK_RE = re.compile(r'<a href="(?P<href>[^"]+)"\s+title="(?P<title>[^"]*)"')
_IMAGE_RE = re.compile(r'<img[^>]*?\ssrc="(?P<src>[^"]+)"')


class LabelEmmausSource(BaseSource):
    name = Source.LABEL_EMMAUS

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.label_emmaus_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_label_emmaus)

    def search(self, query: SearchQuery) -> list[RawListing]:
        params: dict[str, str] = {"q": query.query}
        if query.price_min is not None:
            params["min_prix"] = f"{query.price_min:g}"
        if query.price_max is not None:
            params["max_prix"] = f"{query.price_max:g}"
        sort = _SORTS.get(query.sort)
        if sort:
            params["tri"] = sort
        condition = _CONDITIONS.get(query.condition or "")
        if condition:
            params["etat"] = condition
        resp = httpx.get(
            f"{self._base}/fr/catalogue/",
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
        return parse_catalogue_page(resp.text, self._base)


def parse_catalogue_page(html: str, base_url: str) -> list[RawListing]:
    """Product cards of a catalogue page, in page order (duplicates dropped)."""
    raws: list[RawListing] = []
    seen: set[str] = set()
    cards = list(_CARD_RE.finditer(html))
    for index, card in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else len(html)
        raw = _to_raw(card, html[card.end():end], base_url)
        if raw is not None and raw.source_id not in seen:
            seen.add(raw.source_id)
            raws.append(raw)
    return raws


def _to_raw(card: re.Match, body: str, base_url: str) -> RawListing | None:
    link = _LINK_RE.search(body)
    if link is None:
        return None
    title = unescape(link["title"]).strip()
    if not title:
        return None
    try:
        item = json.loads(unescape(card["data"]))
    except ValueError:
        item = {}
    try:
        price = float(item["price"])
    except (KeyError, TypeError, ValueError):
        price = None
    seller = str(item.get("item_merchant") or "").strip()

    image = _IMAGE_RE.search(body)
    thumb = urljoin(base_url + "/", unescape(image["src"])) if image else None
    return RawListing(
        source=Source.LABEL_EMMAUS,
        source_id=card["id"],
        title=title,
        url=urljoin(base_url + "/", unescape(link["href"])),
        # The card has no description; the seller is the useful extra.
        description=f"Vendu par {seller}" if seller else "",
        price=price,
        currency="EUR",
        thumbnail=thumb,
        photos=[thumb] if thumb else [],
    )
