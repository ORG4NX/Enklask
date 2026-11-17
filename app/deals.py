"""Deal badges ("bonne affaire"): a listing priced well under its search's median.

The reference is the median price of what a saved search has brought in so
far (hidden listings left out: they are mostly off-topic items whose prices
would skew it). A listing priced at DEAL_RATIO of that median or less gets
the badge, unless it is so cheap it is more likely an accessory, a part or a
mistake (below FLOOR_RATIO). Nothing is fetched from outside.
"""

from __future__ import annotations

from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import ListingStatus
from .models import Listing, ListingTopic, SavedSearch

DEAL_RATIO = 0.60  # at most 60 % of the median
FLOOR_RATIO = 0.30  # under 30 %: likely not the same item, no badge
MIN_SAMPLES = 8  # priced listings needed before trusting a median
MAX_SAMPLES = 300  # most recent listings used per search


def search_medians(session: Session, search_ids: set[int]) -> dict[int, tuple[float, int]]:
    """``{search_id: (median price, sample size)}`` for searches with enough data."""
    out: dict[int, tuple[float, int]] = {}
    for sid in search_ids:
        prices = list(session.scalars(
            select(Listing.price)
            .join(ListingTopic, ListingTopic.listing_id == Listing.id)
            .where(
                ListingTopic.search_id == sid,
                Listing.price.is_not(None),
                Listing.price > 0,
                Listing.status != ListingStatus.HIDDEN.value,
            )
            .order_by(Listing.first_seen.desc())
            .limit(MAX_SAMPLES)
        ))
        if len(prices) >= MIN_SAMPLES:
            out[sid] = (median(prices), len(prices))
    return out


def mark_deals(session: Session, search_id: int, items: list, deals_only: bool) -> list:
    """Set ``deal_pct`` on the NotifyItems of new listings from ``search_id``
    and return those to notify: all of them, or only the deals when
    ``deals_only`` (none while the search lacks the prices for a median)."""
    if not items:
        return []
    med = search_medians(session, {search_id}).get(search_id)
    for item in items:
        item.deal_pct = deal_for(item.price, med[0]) if med else None
    if deals_only:
        return [item for item in items if item.deal_pct is not None]
    return list(items)


def deal_for(price: float | None, med: float) -> int | None:
    """Percentage under the median when ``price`` is a deal, else None."""
    if price is None or med <= 0:
        return None
    if FLOOR_RATIO * med <= price <= DEAL_RATIO * med:
        return round(100 * (1 - price / med))
    return None


def annotate_deals(
    session: Session, listings: list[Listing], topic_id: int | None = None
) -> None:
    """Set ``listing.deal`` on each listing: None, or ``{"pct", "median",
    "samples", "search"}`` against the search that brought it in (in
    ``topic_id`` when given; the best-documented search otherwise)."""
    if not listings:
        return
    ids = [li.id for li in listings]
    stmt = select(ListingTopic.listing_id, ListingTopic.search_id).where(
        ListingTopic.listing_id.in_(ids), ListingTopic.search_id.is_not(None)
    )
    if topic_id is not None:
        stmt = stmt.where(ListingTopic.topic_id == topic_id)
    links: dict[int, list[int]] = {}
    for listing_id, search_id in session.execute(stmt):
        links.setdefault(listing_id, []).append(search_id)
    medians = search_medians(session, {s for sids in links.values() for s in sids})
    names = dict(session.execute(
        select(SavedSearch.id, SavedSearch.name).where(SavedSearch.id.in_(list(medians)))
    ).all()) if medians else {}
    for listing in listings:
        listing.deal = None
        known = [s for s in links.get(listing.id, []) if s in medians]
        if not known:
            continue
        sid = max(known, key=lambda s: medians[s][1])
        med, samples = medians[sid]
        pct = deal_for(listing.price, med)
        if pct is not None:
            listing.deal = {
                "pct": pct, "median": med, "samples": samples, "search": names.get(sid, ""),
            }
