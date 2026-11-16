"""Live feed: watch the saved searches on Vinted between regular polls.

The scheduled poll runs every few minutes across every source. The live feed
re-checks each enabled saved search that targets Vinted on its own, much
shorter cycle, through the catalogue API (newest first), and stores whatever it
has not seen yet. The detection follows Vintrack's monitors
(github.com/JakobAIOdev/Vintrack-Vinted-Monitor, MIT): poll the newest page,
keep the unknown ids, alert on those.

* Searches are spread evenly over the interval, so N searches mean one request
  every interval/N seconds rather than a burst.
* The first results of a search are a baseline: stored, never notified, so
  adding a search does not flood the Discord channel with old ads.
* A failure pauses the whole feed with exponential backoff (Vinted refusals
  are per IP, retrying other searches at once would only make it worse).

Only Vinted is watched live: its API is a cheap JSON request. Leboncoin's
DataDome scores frequent requests and would block the IP for every device on
it; eBay, Easy Cash and Label Emmaüs keep the scheduled poll.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .dedup import upsert_listing, write_lock
from .enums import Source
from .models import Listing, ListingTopic, SavedSearch, Topic
from .notifier import Notifier, NotifyItem
from .sources.base import SearchQuery

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 30
MAX_BACKOFF_SECONDS = 15 * 60
# Settings and the search list are re-read at least this often while idle.
_IDLE_RECHECK_SECONDS = 15.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WatchedSearch:
    """Plain snapshot of a SavedSearch + its topic (no live ORM session)."""

    id: int
    name: str
    query: str
    price_min: float | None
    price_max: float | None
    condition: str | None
    tags: list[str]
    topic_id: int
    topic_name: str
    topic_slug: str
    apprise_urls: list[str]


@dataclass
class SearchStatus:
    last_checked_at: datetime | None = None
    last_new_count: int = 0
    last_error: str = ""


@dataclass
class FeedStatus:
    enabled: bool = False
    running: bool = False
    interval_seconds: int = 0
    watched: int | None = None  # None until the first round has listed them
    started_at: datetime | None = None
    last_check_at: datetime | None = None
    last_new_at: datetime | None = None
    new_since_start: int = 0
    last_error: str = ""
    paused_until: datetime | None = None
    searches: dict[int, SearchStatus] = field(default_factory=dict)


def watched_searches() -> list[WatchedSearch]:
    """Enabled searches that include Vinted (no source ticked = all)."""
    out: list[WatchedSearch] = []
    with session_scope() as session:
        rows = session.execute(
            select(SavedSearch, Topic)
            .join(Topic, Topic.id == SavedSearch.topic_id)
            .where(SavedSearch.enabled.is_(True))
            .order_by(Topic.position, Topic.id, SavedSearch.id)
        ).all()
        for search, topic in rows:
            targeted = search.sources or []
            if targeted and Source.VINTED.value not in targeted:
                continue
            out.append(WatchedSearch(
                id=search.id, name=search.name or search.query, query=search.query,
                price_min=search.price_min, price_max=search.price_max,
                condition=search.condition, tags=list(search.tags or []),
                topic_id=topic.id, topic_name=topic.name, topic_slug=topic.slug,
                apprise_urls=topic.apprise_url_list,
            ))
    return out


class LiveFeed:
    def __init__(self, source_factory=None) -> None:
        self._source_factory = source_factory or _vinted_source
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._status = FeedStatus()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status.started_at = _utcnow()
            self._thread = threading.Thread(target=self._run, name="live-feed", daemon=True)
            self._thread.start()
        logger.info("Live feed started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def wake(self) -> None:
        """Re-read the settings and searches now (after a settings change)."""
        self._wake.set()

    def status(self) -> FeedStatus:
        with self._lock:
            settings = get_settings()
            self._status.enabled = bool(settings.enable_live_feed and settings.enable_vinted)
            self._status.interval_seconds = interval_seconds()
            self._status.running = self._thread is not None and self._thread.is_alive()
            return FeedStatus(**{
                **self._status.__dict__,
                "searches": dict(self._status.searches),
            })

    # -- loop ----------------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(seconds, 0.0))
        self._wake.clear()

    def _run(self) -> None:
        # Let the app finish starting (and the first scheduled poll begin).
        self._wait(random.uniform(5, 15))
        while not self._stop.is_set():
            try:
                self._round()
            except Exception:  # pragma: no cover - the loop must never die
                logger.exception("Live feed round failed")
                self._wait(_IDLE_RECHECK_SECONDS)

    def _round(self) -> None:
        settings = get_settings()
        if not (settings.enable_live_feed and settings.enable_vinted):
            self._wait(_IDLE_RECHECK_SECONDS)
            return
        paused = self._paused_for()
        if paused > 0:
            self._wait(min(paused, _IDLE_RECHECK_SECONDS))
            return
        searches = watched_searches()
        with self._lock:
            self._status.watched = len(searches)
        if not searches:
            self._wait(_IDLE_RECHECK_SECONDS)
            return
        spacing = interval_seconds() / len(searches)
        for search in searches:
            if self._stop.is_set():
                return
            if not self.check(search):
                return  # paused: start over once the backoff is spent
            # +-20 % jitter so requests never fall on an exact rhythm.
            self._wait(spacing * random.uniform(0.8, 1.2))

    def _paused_for(self) -> float:
        with self._lock:
            until = self._status.paused_until
        if until is None:
            return 0.0
        return (until - _utcnow()).total_seconds()

    # -- one search ----------------------------------------------------------

    def check(self, search: WatchedSearch) -> bool:
        """Fetch one search, store and notify the new ads. False on failure."""
        source = self._source_factory()
        query = SearchQuery(
            query=search.query, price_min=search.price_min,
            price_max=search.price_max, condition=search.condition,
        )
        started = time.monotonic()
        try:
            raws = source.search_api(query)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(search, exc)
            return False

        with write_lock, session_scope() as session:
            baseline = _is_baseline(session, search.id)
            new_items: list[NotifyItem] = []
            for raw in raws:
                listing = upsert_listing(
                    session, raw, topic_id=search.topic_id,
                    search_id=search.id, tags=search.tags,
                )
                if listing is not None:
                    new_items.append(NotifyItem.from_listing(listing, topic_name=search.topic_name))

        self._record_success(search, len(new_items))
        if new_items:
            logger.info(
                "Live feed: %d new on '%s'%s (%.1fs)", len(new_items), search.name,
                " (baseline, not notified)" if baseline else "", time.monotonic() - started,
            )
        if new_items and not baseline:
            try:
                Notifier.for_topic(search.apprise_urls).notify_new(new_items)
            except Exception:  # noqa: BLE001 - a failed webhook must not stop the feed
                logger.exception("Live feed notification failed for '%s'", search.name)
        return True

    def _record_success(self, search: WatchedSearch, new_count: int) -> None:
        now = _utcnow()
        with self._lock:
            self._failures = 0
            st = self._status
            st.last_check_at = now
            st.last_error = ""
            st.paused_until = None
            if new_count:
                st.last_new_at = now
                st.new_since_start += new_count
            st.searches[search.id] = SearchStatus(now, new_count, "")

    def _record_failure(self, search: WatchedSearch, exc: Exception) -> None:
        message = str(exc) or type(exc).__name__
        with self._lock:
            self._failures += 1
            delay = min(interval_seconds() * 2 ** (self._failures - 1), MAX_BACKOFF_SECONDS)
            st = self._status
            st.last_check_at = _utcnow()
            st.last_error = message
            st.paused_until = _utcnow() + timedelta(seconds=delay)
            st.searches[search.id] = SearchStatus(st.last_check_at, 0, message)
        logger.warning(
            "Live feed: '%s' failed (%s); pausing %ds", search.name, message, delay
        )


def _is_baseline(session, search_id: int) -> bool:
    """True while this search has never stored a Vinted ad (first fetch)."""
    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .join(Listing, Listing.id == ListingTopic.listing_id)
        .where(ListingTopic.search_id == search_id, Listing.source == Source.VINTED.value)
    )
    return not count


def interval_seconds() -> int:
    return max(int(get_settings().live_feed_interval_seconds), MIN_INTERVAL_SECONDS)


def _vinted_source():
    from .sources.vinted import VintedSource

    return VintedSource()


live_feed = LiveFeed()


def start_live_feed() -> None:
    live_feed.start()


def stop_live_feed() -> None:
    live_feed.stop()
