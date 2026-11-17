"""Live feed: watch the saved searches between regular polls.

The scheduled poll runs every few minutes across every source. The live feed
re-checks each enabled saved search on a shorter cycle, newest first, stores
what it has not seen yet and notifies it. The detection follows Vintrack's
monitors (github.com/JakobAIOdev/Vintrack-Vinted-Monitor, MIT): poll the
newest page, keep the unknown ids, alert on those.

Each source is a *lane* with its own thread, interval and backoff, so a
blocked source never holds up the others:

* **Vinted** — the catalogue JSON API, a cheap request: every 60 s by default.
* **Leboncoin** — off by default, every 5 min at least 3. It goes through
  ``curl_cffi`` only and never escalates to the Camoufox browser; it also
  stands aside while a poll runs. DataDome scores request frequency per IP,
  and repeated challenges taint the IP for every device behind it.

Shared rules:

* Searches are spread evenly over the interval (N searches -> one request
  every interval/N), with +-20 % jitter.
* The first results of a search on a source are a baseline: stored, never
  notified, so adding a search does not flood the Discord channel.
* A failure pauses the lane with exponential backoff: refusals are per IP, so
  trying the next search at once would only make it worse.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .config import get_settings
from . import health
from .db import session_scope
from .dedup import upsert_listing, write_lock
from .enums import Source
from .filters import drop_excluded, drop_missing_required, drop_off_query
from .jobs import jobs
from .models import Listing, ListingTopic, SavedSearch, Topic
from .deals import mark_deals
from .notifier import Notifier, NotifyItem
from .price_alerts import check_price_targets
from .sources.base import SearchQuery

logger = logging.getLogger(__name__)

# Settings and the search list are re-read at least this often while idle.
_IDLE_RECHECK_SECONDS = 15.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LaneSpec:
    """How one source takes part in the live feed."""

    source: Source
    label: str
    enabled_setting: str         # live feed toggle for this source
    source_setting: str          # the source itself must be on too
    interval_setting: str
    min_interval: int
    max_backoff: int
    fetch: Callable[[SearchQuery], list]
    # Stand aside while a poll is running (avoids doubling requests).
    yield_to_polls: bool = False

    def enabled(self, settings) -> bool:
        return bool(getattr(settings, self.enabled_setting)) and bool(
            getattr(settings, self.source_setting)
        )

    def interval(self, settings) -> int:
        return max(int(getattr(settings, self.interval_setting)), self.min_interval)


@dataclass
class WatchedSearch:
    """Plain snapshot of a SavedSearch + its topic (no live ORM session)."""

    id: int
    name: str
    query: str
    price_min: float | None
    price_max: float | None
    max_distance_km: float | None
    condition: str | None
    tags: list[str]
    exclude_words: list[str]
    required_words: list[str]
    topic_id: int
    topic_name: str
    topic_slug: str
    apprise_urls: list[str]
    notify_deals_only: bool = False
    strict_titles: bool = False


@dataclass
class SearchStatus:
    last_checked_at: datetime | None = None
    last_new_count: int = 0
    last_error: str = ""


@dataclass
class LaneStatus:
    source: str = ""
    label: str = ""
    enabled: bool = False
    running: bool = False
    interval_seconds: int = 0
    watched: int | None = None  # None until the first round has listed them
    last_check_at: datetime | None = None
    last_new_at: datetime | None = None
    new_since_start: int = 0
    last_error: str = ""
    paused_until: datetime | None = None
    yielding: bool = False      # waiting for a running poll to finish
    searches: dict[int, SearchStatus] = field(default_factory=dict)


def watched_searches(source: Source = Source.VINTED) -> list[WatchedSearch]:
    """Enabled searches that include ``source`` (no source ticked = all)."""
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
            if targeted and source.value not in targeted:
                continue
            out.append(WatchedSearch(
                id=search.id, name=search.name or search.query, query=search.query,
                price_min=search.price_min, price_max=search.price_max,
                max_distance_km=search.max_distance_km,
                condition=search.condition, tags=list(search.tags or []),
                exclude_words=list(search.exclude_words or []),
                required_words=list(search.required_words or []),
                notify_deals_only=bool(search.notify_deals_only),
                strict_titles=bool(search.strict_titles),
                topic_id=topic.id, topic_name=topic.name, topic_slug=topic.slug,
                apprise_urls=topic.apprise_url_list,
            ))
    return out


class Lane:
    """One source's live loop: its own thread, interval, backoff and status."""

    def __init__(self, spec: LaneSpec) -> None:
        self.spec = spec
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._status = LaneStatus(source=spec.source.value, label=spec.label)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"live-{self.spec.source.value}", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def status(self) -> LaneStatus:
        settings = get_settings()
        with self._lock:
            st = self._status
            st.enabled = self.spec.enabled(settings)
            st.interval_seconds = self.spec.interval(settings)
            st.running = self._thread is not None and self._thread.is_alive()
            return LaneStatus(**{**st.__dict__, "searches": dict(st.searches)})

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
                logger.exception("Live feed (%s) round failed", self.spec.label)
                self._wait(_IDLE_RECHECK_SECONDS)

    def _round(self) -> None:
        settings = get_settings()
        if not self.spec.enabled(settings):
            self._wait(_IDLE_RECHECK_SECONDS)
            return
        paused = self._paused_for()
        if paused > 0:
            self._wait(min(paused, _IDLE_RECHECK_SECONDS))
            return
        if self._should_yield():
            self._wait(_IDLE_RECHECK_SECONDS)
            return
        searches = watched_searches(self.spec.source)
        with self._lock:
            self._status.watched = len(searches)
        if not searches:
            self._wait(_IDLE_RECHECK_SECONDS)
            return
        spacing = self.spec.interval(settings) / len(searches)
        for search in searches:
            if self._stop.is_set() or not self.spec.enabled(get_settings()):
                return
            if self._should_yield():
                return  # a poll started: it covers this source, start over after
            if not self.check(search):
                return  # paused: start over once the backoff is spent
            self._wait(spacing * random.uniform(0.8, 1.2))

    def _should_yield(self) -> bool:
        yielding = self.spec.yield_to_polls and jobs.any_running()
        with self._lock:
            self._status.yielding = yielding
        return yielding

    def _paused_for(self) -> float:
        with self._lock:
            until = self._status.paused_until
        if until is None:
            return 0.0
        return (until - _utcnow()).total_seconds()

    # -- one search ----------------------------------------------------------

    def check(self, search: WatchedSearch) -> bool:
        """Fetch one search, store and notify the new ads. False on failure."""
        query = SearchQuery(
            query=search.query, price_min=search.price_min,
            price_max=search.price_max, condition=search.condition,
            max_distance_km=search.max_distance_km,
        )
        started = time.monotonic()
        try:
            raws = self.spec.fetch(query)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(search, exc)
            health.record(self.spec.source.value, error=exc)
            return False
        health.record(self.spec.source.value, count=len(raws))
        raws = drop_excluded(raws, search.exclude_words)
        raws = drop_missing_required(raws, search.required_words)
        if search.strict_titles:
            raws = drop_off_query(raws, search.query)

        with write_lock, session_scope() as session:
            baseline = _is_baseline(session, search.id, self.spec.source)
            new_items: list[NotifyItem] = []
            for raw in raws:
                listing = upsert_listing(
                    session, raw, topic_id=search.topic_id,
                    search_id=search.id, tags=search.tags,
                )
                if listing is not None:
                    new_items.append(NotifyItem.from_listing(listing, topic_name=search.topic_name))
            new_count = len(new_items)
            to_notify = mark_deals(session, search.id, new_items, search.notify_deals_only)

        self._record_success(search, new_count)
        check_price_targets()  # a followed listing's price may have moved
        if new_items:
            logger.info(
                "Live feed (%s): %d new on '%s'%s (%.1fs)", self.spec.label,
                new_count, search.name,
                " (baseline, not notified)" if baseline else "", time.monotonic() - started,
            )
        if to_notify and not baseline:
            try:
                Notifier.for_topic(search.apprise_urls).notify_new(to_notify)
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
        interval = self.spec.interval(get_settings())
        with self._lock:
            self._failures += 1
            delay = min(interval * 2 ** (self._failures - 1), self.spec.max_backoff)
            st = self._status
            st.last_check_at = _utcnow()
            st.last_error = message
            st.paused_until = st.last_check_at + timedelta(seconds=delay)
            st.searches[search.id] = SearchStatus(st.last_check_at, 0, message)
        logger.warning(
            "Live feed (%s): '%s' failed (%s); pausing %ds",
            self.spec.label, search.name, message, delay,
        )


def _is_baseline(session, search_id: int, source: Source) -> bool:
    """True while this search has never stored an ad from ``source``."""
    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .join(Listing, Listing.id == ListingTopic.listing_id)
        .where(ListingTopic.search_id == search_id, Listing.source == source.value)
    )
    return not count


def _fetch_vinted(query: SearchQuery) -> list:
    from .sources.vinted import VintedSource

    return VintedSource().search_api(query)


def _fetch_leboncoin(query: SearchQuery) -> list:
    from .sources.leboncoin import LeboncoinSource

    return LeboncoinSource().search_live(query)


VINTED_LANE = LaneSpec(
    source=Source.VINTED, label="Vinted",
    enabled_setting="enable_live_feed", source_setting="enable_vinted",
    interval_setting="live_feed_interval_seconds", min_interval=30,
    max_backoff=15 * 60, fetch=_fetch_vinted,
)
LEBONCOIN_LANE = LaneSpec(
    source=Source.LEBONCOIN, label="Leboncoin",
    enabled_setting="enable_live_feed_leboncoin", source_setting="enable_leboncoin",
    interval_setting="live_feed_leboncoin_interval_seconds", min_interval=180,
    max_backoff=60 * 60, fetch=_fetch_leboncoin, yield_to_polls=True,
)


class LiveFeed:
    """Every lane together (one thread each)."""

    def __init__(self, specs: list[LaneSpec] | None = None) -> None:
        self.lanes = [Lane(spec) for spec in (specs or [VINTED_LANE, LEBONCOIN_LANE])]

    def lane(self, source: Source) -> Lane:
        return next(lane for lane in self.lanes if lane.spec.source is source)

    def start(self) -> None:
        for lane in self.lanes:
            lane.start()
        logger.info("Live feed started: %s", ", ".join(l.spec.label for l in self.lanes))

    def stop(self) -> None:
        for lane in self.lanes:
            lane.stop()

    def wake(self) -> None:
        """Re-read the settings and searches now (after a settings change)."""
        for lane in self.lanes:
            lane.wake()

    def status(self) -> list[LaneStatus]:
        return [lane.status() for lane in self.lanes]


live_feed = LiveFeed()


def start_live_feed() -> None:
    live_feed.start()


def stop_live_feed() -> None:
    live_feed.stop()
