import dataclasses
from datetime import datetime, timezone
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import livefeed
from app.db import get_session
from app.enums import Source
from app.livefeed import Lane, LiveFeed, watched_searches
from app.main import app
from app.models import Base, Listing, ListingTopic, SavedSearch, Topic
from app.sources.base import RawListing
from app.web import routes as web_routes


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def scope():
        session = make()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(livefeed, "session_scope", scope)
    return make


@pytest.fixture
def notified(monkeypatch):
    sent = []

    class _Notifier:
        @classmethod
        def for_topic(cls, urls):
            return cls()

        def notify_new(self, items, is_cold_start=False):
            sent.append([item.title for item in items])

    monkeypatch.setattr(livefeed, "Notifier", _Notifier)
    return sent


def _raw(item_id, title=None, price=10.0, source=Source.VINTED):
    return RawListing(
        source=source, source_id=str(item_id), title=title or f"Ad {item_id}",
        url=f"https://example.test/items/{item_id}", price=price,
    )


class _Fetch:
    def __init__(self, *rounds, error=None):
        self.rounds, self.error, self.queries = list(rounds), error, []

    def __call__(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.rounds.pop(0)


def _lane(fetch, base=livefeed.VINTED_LANE):
    return Lane(dataclasses.replace(base, fetch=fetch))


def _seed(make, sources=None):
    with make() as session:
        topic = Topic(name="Nintendo", slug="nintendo")
        session.add(topic)
        session.flush()
        search = SavedSearch(
            topic_id=topic.id, name="New 3DS", query="New 3DS", price_max=150.0,
            tags=["3ds"], sources=sources or [],
        )
        session.add(search)
        session.commit()
        return topic.id, search.id


def test_watched_searches_keep_only_those_reaching_the_source(factory):
    _seed(factory)
    lbc = SavedSearch(topic_id=1, name="LBC", query="x", sources=["leboncoin"])
    disabled = SavedSearch(topic_id=1, name="Off", query="y", enabled=False)
    targeted = SavedSearch(topic_id=1, name="Vinted", query="z", sources=["vinted"])
    with factory() as session:
        session.add_all([lbc, disabled, targeted])
        session.commit()

    assert [s.name for s in watched_searches(Source.VINTED)] == ["New 3DS", "Vinted"]
    assert [s.name for s in watched_searches(Source.LEBONCOIN)] == ["New 3DS", "LBC"]


def test_first_results_are_a_baseline_then_new_ads_are_notified(factory, notified):
    _, search_id = _seed(factory)
    fetch = _Fetch([_raw(1), _raw(2)], [_raw(3, "Fresh 3DS"), _raw(2), _raw(1)])
    lane = _lane(fetch)
    search = watched_searches()[0]

    assert lane.check(search) is True
    assert notified == []  # baseline: stored, not announced
    assert lane.check(search) is True
    assert notified == [["Fresh 3DS"]]

    assert fetch.queries[0].query == "New 3DS" and fetch.queries[0].price_max == 150.0
    with factory() as session:
        assert session.scalar(select(Listing).where(Listing.source_id == "3")) is not None
        link = session.scalar(select(ListingTopic).where(ListingTopic.search_id == search_id))
        assert link.tags == ["3ds"]
    status = lane.status()
    assert status.new_since_start == 3 and status.searches[search_id].last_new_count == 1


def test_baseline_is_per_source(factory, notified):
    _seed(factory)
    search = watched_searches()[0]
    _lane(_Fetch([_raw(1)])).check(search)
    # Vinted already has ads for this search; Leboncoin's first batch is still
    # a baseline and must not be announced.
    lbc = _lane(_Fetch([_raw(9, source=Source.LEBONCOIN)]), livefeed.LEBONCOIN_LANE)
    assert lbc.check(search) is True
    assert notified == []


def test_a_failure_pauses_only_that_lane_with_backoff(factory, notified):
    _seed(factory)
    lane = _lane(_Fetch(error=RuntimeError("HTTP 403 DataDome device check")),
                 livefeed.LEBONCOIN_LANE)
    search = watched_searches()[0]

    assert lane.check(search) is False
    first = lane.status().paused_until
    assert "DataDome" in lane.status().last_error and lane._paused_for() > 0
    lane.check(search)
    assert lane.status().paused_until > first  # backoff doubles
    vinted = _lane(_Fetch([_raw(1)]))
    assert vinted.status().paused_until is None


def test_round_is_idle_when_disabled(factory, monkeypatch):
    _seed(factory)
    settings = livefeed.get_settings().model_copy(update={"enable_live_feed": False})
    monkeypatch.setattr(livefeed, "get_settings", lambda: settings)
    lane = _lane(lambda q: pytest.fail("no request while disabled"))
    monkeypatch.setattr(lane, "_wait", lambda seconds: None)
    lane._round()
    assert lane.status().enabled is False


def test_leboncoin_lane_is_off_by_default_slower_and_yields_to_polls(factory, monkeypatch):
    _seed(factory)
    settings = livefeed.get_settings().model_copy(update={
        "enable_leboncoin": True, "enable_live_feed_leboncoin": True,
        "live_feed_leboncoin_interval_seconds": 60,
    })
    monkeypatch.setattr(livefeed, "get_settings", lambda: settings)
    from app.config import Settings

    assert Settings().enable_live_feed_leboncoin is False
    lane = _lane(lambda q: pytest.fail("no request while a poll runs"), livefeed.LEBONCOIN_LANE)
    assert lane.status().interval_seconds == 180  # clamped to the minimum
    monkeypatch.setattr(livefeed.jobs, "any_running", lambda: True)
    monkeypatch.setattr(lane, "_wait", lambda seconds: None)
    lane._round()
    assert lane.status().yielding is True


def test_leboncoin_live_search_never_uses_the_browser(monkeypatch):
    from app.sources import leboncoin

    src = leboncoin.LeboncoinSource()
    monkeypatch.setattr(src, "_post_browser", lambda body: pytest.fail("browser used"))
    monkeypatch.setattr(src, "_post_curl", lambda body: {"ads": []})
    assert src.search_live(livefeed.SearchQuery("3ds")) == []

    monkeypatch.setattr(leboncoin, "_browser_preferred_until", float("inf"))
    with pytest.raises(leboncoin.DataDomeBlocked):
        src.search_live(livefeed.SearchQuery("3ds"))


# -- web page -------------------------------------------------------------


@pytest.fixture
def client(factory):
    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _listing(make, item_id, title):
    with make() as session:
        listing = Listing(
            source="vinted", source_id=str(item_id), title=title,
            url=f"https://www.vinted.fr/items/{item_id}", price=42.0, currency="EUR",
        )
        session.add(listing)
        session.flush()
        session.add(ListingTopic(listing_id=listing.id, topic_id=1, search_id=1))
        session.commit()
        return listing.id


def test_live_page_lists_newest_first_with_their_search(client, factory):
    _seed(factory)
    _listing(factory, 1, "Older ad")
    newest = _listing(factory, 2, "Newest ad")

    html = client.get("/live").text
    assert html.index("Newest ad") < html.index("Older ad")
    assert "Nintendo &middot; New 3DS" in html
    assert f"/partials/live?after={newest}" in html


def test_live_partial_returns_only_newer_ads_and_moves_the_cursor(client, factory):
    _seed(factory)
    first = _listing(factory, 1, "Seen ad")
    second = _listing(factory, 2, "Brand new ad")

    html = client.get(f"/partials/live?after={first}").text
    assert "Brand new ad" in html and "Seen ad" not in html
    assert "live-row-new" in html
    assert 'id="live-poller"' in html and 'hx-swap-oob="true"' in html
    assert f"after={second}" in html

    empty = client.get(f"/partials/live?after={second}").text
    assert "listing-card" not in empty
    assert f"after={second}" in empty  # cursor kept


def test_settings_save_live_feed_fields(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(web_routes, "save_settings", lambda u: saved.update(u))
    monkeypatch.setattr(web_routes, "reschedule_poll", lambda _m: None)

    too_fast = client.post(
        "/settings", data={"live_feed_interval_seconds": "5"}, follow_redirects=False
    )
    assert too_fast.status_code == 400 and "au moins 30" in too_fast.text

    ok = client.post(
        "/settings",
        data={"enable_live_feed": "on", "live_feed_interval_seconds": "45"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert saved["enable_live_feed"] is True and saved["live_feed_interval_seconds"] == 45
    assert 'name="enable_live_feed"' in client.get("/settings").text


def test_header_highlights_the_current_page_icon(client, factory):
    _seed(factory)
    for path in ("/live", "/liked", "/settings"):
        html = client.get(path).text
        assert html.count('aria-current="page"') == 1
        assert f'href="{path}" title=' in html.split('aria-current="page"')[0].rsplit("<a ", 1)[1]
        assert "bg-brand-light text-brand flex" in html  # title badge, same glyph
    assert 'aria-current="page"' not in client.get("/").text


def test_status_says_starting_before_the_first_round(client, factory, monkeypatch):
    monkeypatch.setattr(web_routes, "live_feed", LiveFeed())
    settings = livefeed.get_settings().model_copy(
        update={"enable_live_feed": True, "enable_vinted": True}
    )
    monkeypatch.setattr(livefeed, "get_settings", lambda: settings)
    html = client.get("/live").text
    assert "Démarrage" in html and "Aucune recherche active" not in html
    # One line per lane; Leboncoin is off unless both toggles are on.
    assert "Leboncoin" in html and "Désactivé" in html


def test_settings_save_leboncoin_live_fields(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(web_routes, "save_settings", lambda u: saved.update(u))
    monkeypatch.setattr(web_routes, "reschedule_poll", lambda _m: None)

    too_fast = client.post(
        "/settings", data={"live_feed_leboncoin_interval_seconds": "60"}, follow_redirects=False
    )
    assert too_fast.status_code == 400 and "au moins 180" in too_fast.text

    ok = client.post(
        "/settings",
        data={"enable_live_feed_leboncoin": "on", "live_feed_leboncoin_interval_seconds": "300"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert saved["enable_live_feed_leboncoin"] is True
    assert saved["live_feed_leboncoin_interval_seconds"] == 300
    html = client.get("/settings").text
    assert 'name="enable_live_feed_leboncoin"' in html and "Direct Leboncoin" in html


def test_duration_and_timeuntil_filters():
    from datetime import timedelta

    assert web_routes._duration(60) == "60 s" and web_routes._duration(300) == "5 min"
    soon = datetime.now(timezone.utc) + timedelta(minutes=4, seconds=30)
    assert web_routes._timeuntil(soon) == "dans 4 min"
