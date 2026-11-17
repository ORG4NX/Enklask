"""Target-price alerts on followed listings.

A followed ("liked") listing can carry a target price. After each poll and
live-feed check, every followed listing whose price is at or below its target
and has not been alerted yet is notified once, then marked; it re-arms when
the price climbs back above the target, so a later drop alerts again.

Selection and marking happen in one transaction under the shared write lock,
so the poller and the live feed (separate threads) never alert twice.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .db import session_scope
from .dedup import write_lock
from .enums import ListingStatus
from .models import Listing, ListingTopic, Topic
from .notifier import Notifier, NotifyItem

logger = logging.getLogger(__name__)


def target_reached(listing: Listing) -> bool:
    return (
        listing.target_price is not None
        and listing.price is not None
        and listing.price <= listing.target_price
    )


def check_price_targets() -> int:
    """Notify the followed listings that just reached their target price.

    Returns how many alerts were handed to a notifier. Never raises: an alert
    failure must not break the poll or the live feed.
    """
    try:
        pending = _collect_and_mark()
    except Exception:  # noqa: BLE001
        logger.exception("Price target check failed")
        return 0
    sent = 0
    for item, target, urls in pending:
        try:
            if Notifier.for_topic(urls).notify_price_target(item, target):
                sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("Price target alert failed for '%s'", item.title)
    return sent


def _collect_and_mark() -> list[tuple[NotifyItem, float, list[str]]]:
    pending: list[tuple[NotifyItem, float, list[str]]] = []
    with write_lock, session_scope() as session:
        listings = session.scalars(
            select(Listing).where(
                Listing.status == ListingStatus.LIKED.value,
                Listing.target_price.is_not(None),
            )
        )
        for listing in listings:
            reached = target_reached(listing)
            if reached and not listing.target_alerted:
                listing.target_alerted = True
                topic = session.scalar(
                    select(Topic)
                    .join(ListingTopic, ListingTopic.topic_id == Topic.id)
                    .where(ListingTopic.listing_id == listing.id)
                    .limit(1)
                )
                pending.append((
                    NotifyItem.from_listing(listing, topic_name=topic.name if topic else ""),
                    listing.target_price,
                    topic.apprise_url_list if topic else [],
                ))
            elif not reached and listing.target_alerted and listing.price is not None:
                listing.target_alerted = False  # back above: re-arm
    return pending
