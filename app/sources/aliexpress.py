"""AliExpress source adapter (unofficial, server-rendered search page).

Meant for repair and modding parts (replacement shells, IPS screen kits,
optical drive emulators, batteries...), which are new products sold at a
fixed price, not second-hand ads. The search page embeds its product cards
as JSON (``"itemList":{"content":[...]}``): id, title, sale price, photo,
rating, units sold and selling-point tags such as "Livraison gratuite".

The site's search is loose ("saturn odd" brings back Elegoo Saturn 3D
printers), so only titles containing every searched word are kept. Fetched
through curl_cffi with a browser TLS fingerprint; a captcha ("punish") page
raises AliExpressBlocked so the health light shows the block.
Best-effort and isolated like the other unofficial sources.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from urllib.parse import quote

from ..config import get_settings
from ..enums import Source
from ..filters import drop_off_query
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - curl_cffi is a regular dependency
    cffi_requests = None

# Our generic sort -> the site's ``SortType`` (absent = best match). "recent"
# stays on best match: a fixed catalogue has no meaningful "newest" order and
# the loose search would fill the page with noise.
_SORTS = {"price_asc": "price_asc", "price_desc": "price_desc"}

_ITEMS_KEY = '"itemList":{"content":'
_BLOCKED_RE = re.compile(r"punish|captcha|x5sec|slide to verify", re.I)
# After a captcha, leave the site alone for a while: insisting only extends
# the block (it showed up after ~25 searches in 10 minutes while testing).
BLOCK_COOLDOWN_SECONDS = 30 * 60
_blocked_until = 0.0
_block_lock = threading.Lock()

_SHIPPING_RE = re.compile(r"livraison[^0-9]{0,12}([0-9]+(?:[.,][0-9]{1,2})?)\s*€", re.I)


class AliExpressBlocked(RuntimeError):
    """The site answered with its anti-bot page instead of results."""


class AliExpressSource(BaseSource):
    name = Source.ALIEXPRESS
    opt_in = True  # parts catalogue: only for searches that tick it

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.aliexpress_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_aliexpress)

    def search(self, query: SearchQuery) -> list[RawListing]:
        # Everything here is new: a search for second-hand condition only
        # would return nothing relevant.
        if query.condition and query.condition != "new":
            return []
        if cffi_requests is None:
            raise RuntimeError("AliExpress needs curl_cffi (pip install curl_cffi)")
        remaining = _blocked_until - time.monotonic()
        if remaining > 0:
            raise AliExpressBlocked(
                f"AliExpress en pause après un captcha (cooldown, encore {remaining / 60:.0f} min)"
            )
        params: dict[str, str] = {"SearchText": query.query}
        if query.price_min is not None:
            params["minPrice"] = f"{query.price_min:g}"
        if query.price_max is not None:
            params["maxPrice"] = f"{query.price_max:g}"
        sort = _SORTS.get(query.sort)
        if sort:
            params["SortType"] = sort
        slug = quote(re.sub(r"\s+", "-", query.query.strip()), safe="-")
        resp = cffi_requests.get(
            f"{self._base}/w/wholesale-{slug}.html",
            params=params,
            impersonate="chrome",
            headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            timeout=30,
        )
        if resp.status_code in (403, 429):
            _start_cooldown()
            raise AliExpressBlocked(f"AliExpress HTTP {resp.status_code}")
        resp.raise_for_status()
        html = resp.text
        if _ITEMS_KEY not in html and _BLOCKED_RE.search(html):
            _start_cooldown()
            raise AliExpressBlocked("AliExpress captcha (punish page)")
        return drop_off_query(parse_search_page(html, self._base), query.query)


def _start_cooldown() -> None:
    global _blocked_until
    with _block_lock:
        _blocked_until = time.monotonic() + BLOCK_COOLDOWN_SECONDS
    logger.warning("AliExpress: captcha, pausing for %d min", BLOCK_COOLDOWN_SECONDS // 60)


def reset_cooldown() -> None:
    """Forget a previous block (tests)."""
    global _blocked_until
    with _block_lock:
        _blocked_until = 0.0


def parse_search_page(html: str, base_url: str = "https://fr.aliexpress.com") -> list[RawListing]:
    """Product cards of a search page, in page order (duplicates dropped)."""
    start = html.find(_ITEMS_KEY)
    if start < 0:
        return []
    try:
        items, _ = json.JSONDecoder().raw_decode(html, start + len(_ITEMS_KEY))
    except ValueError:
        logger.warning("AliExpress: product list is not valid JSON")
        return []
    raws: list[RawListing] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        raw = _to_raw(item, base_url.rstrip("/"))
        if raw is not None and raw.source_id not in seen:
            seen.add(raw.source_id)
            raws.append(raw)
    return raws


def _to_raw(item: dict, base_url: str) -> RawListing | None:
    if not isinstance(item, dict):
        return None
    product_id = str(item.get("productId") or "").strip()
    title = ((item.get("title") or {}).get("displayTitle") or "").strip()
    if not product_id.isdigit() or not title:
        return None

    sale = (item.get("prices") or {}).get("salePrice") or {}
    try:
        price = float(sale["minPrice"])
    except (KeyError, TypeError, ValueError):
        price = None
    currency = sale.get("currencyCode") or "EUR"

    image = (item.get("image") or {}).get("imgUrl") or ""
    if image.startswith("//"):
        image = "https:" + image
    photos = [image] if image else []

    tags = [
        ((sp.get("tagContent") or {}).get("tagText") or "").strip()
        for sp in item.get("sellingPoints") or []
        if isinstance(sp, dict)
    ]
    tags = [t for t in tags if t]
    shipping_cost = None
    for tag in tags:
        if "livraison gratuite" in tag.lower():
            shipping_cost = 0.0
            break
        m = _SHIPPING_RE.search(tag)
        if m:
            shipping_cost = float(m.group(1).replace(",", "."))
            break

    # Rating, units sold and the "lowest price in 30 days" tag help judge an
    # offer; they stand in for the description the card does not have.
    details: list[str] = []
    rating = (item.get("evaluation") or {}).get("starRating")
    if rating:
        details.append(f"{rating}★")
    sold = (item.get("trade") or {}).get("tradeDesc")
    if sold:
        details.append(str(sold))
    details += [t for t in tags if "30 derniers jours" in t]

    return RawListing(
        source=Source.ALIEXPRESS,
        source_id=product_id,
        title=title,
        url=f"{base_url}/item/{product_id}.html",
        description=" · ".join(details),
        price=price,
        currency=currency,
        thumbnail=image or None,
        photos=photos,
        shipping_cost=shipping_cost,
    )
