from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.price_alerts as price_alerts
from app import migrate
from app.enums import ListingStatus
from app.models import Base, Listing, ListingTopic, Topic


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(price_alerts, "session_scope", scope)
    return factory


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class FakeNotifier:
        def __init__(self, urls):
            self.urls = urls

        def notify_price_target(self, item, target):
            calls.append((item.title, item.price, target, self.urls, item.topic_name))
            return True

    monkeypatch.setattr(price_alerts.Notifier, "for_topic", lambda urls: FakeNotifier(urls))
    return calls


def _followed(session, price, target, status=ListingStatus.LIKED.value):
    topic = Topic(name="Consoles", slug="consoles", apprise_urls="ntfy://x/y")
    session.add(topic)
    session.flush()
    listing = Listing(source="vinted", source_id="9", title="Game Boy Player",
                      url="https://x/9", price=price, target_price=target, status=status)
    session.add(listing)
    session.flush()
    session.add(ListingTopic(listing_id=listing.id, topic_id=topic.id))
    session.commit()
    return listing


def _set_price(factory, listing_id, price):
    with factory() as session:
        session.get(Listing, listing_id).price = price
        session.commit()


def test_alerts_once_when_the_price_reaches_the_target_then_rearms(factory, sent):
    with factory() as session:
        listing_id = _followed(session, price=80.0, target=50.0).id

    assert price_alerts.check_price_targets() == 0  # still above

    _set_price(factory, listing_id, 45.0)
    assert price_alerts.check_price_targets() == 1
    assert sent == [("Game Boy Player", 45.0, 50.0, ["ntfy://x/y"], "Consoles")]
    assert price_alerts.check_price_targets() == 0  # not twice

    _set_price(factory, listing_id, 70.0)  # back above: re-armed
    assert price_alerts.check_price_targets() == 0
    _set_price(factory, listing_id, 50.0)  # target itself counts
    assert price_alerts.check_price_targets() == 1
    assert len(sent) == 2


def test_listings_not_followed_or_without_target_are_ignored(factory, sent):
    with factory() as session:
        _followed(session, price=10.0, target=50.0, status=ListingStatus.SEEN.value)
    assert price_alerts.check_price_targets() == 0
    assert sent == []


def test_a_failing_notifier_never_raises(factory, monkeypatch):
    with factory() as session:
        _followed(session, price=10.0, target=50.0)

    def boom(urls):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(price_alerts.Notifier, "for_topic", boom)
    assert price_alerts.check_price_targets() == 0


def test_migration_adds_the_target_columns(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE listings (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO listings (id) VALUES (1)"))
    monkeypatch.setattr(migrate, "engine", engine)
    migrate._ensure_listing_columns()
    migrate._ensure_listing_columns()  # idempotent
    columns = {c["name"] for c in inspect(engine).get_columns("listings")}
    assert {"target_price", "target_alerted"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT target_alerted FROM listings")).scalar() == 0


def _set_available(factory, listing_id, available):
    with factory() as session:
        for link in session.query(ListingTopic).filter_by(listing_id=listing_id):
            link.is_available = available
        session.commit()


def test_gone_followed_listing_alerts_once_then_rearms(factory, monkeypatch):
    gone = []

    class FakeNotifier:
        def notify_gone(self, item):
            gone.append(item.title)
            return True

    monkeypatch.setattr(price_alerts.Notifier, "for_topic", lambda urls: FakeNotifier())
    with factory() as session:
        listing_id = _followed(session, price=80.0, target=None).id

    assert price_alerts.check_gone_listings() == 0  # still online
    _set_available(factory, listing_id, False)
    assert price_alerts.check_gone_listings() == 1
    assert gone == ["Game Boy Player"]
    assert price_alerts.check_gone_listings() == 0  # not twice
    _set_available(factory, listing_id, True)
    assert price_alerts.check_gone_listings() == 0  # back: re-armed
    _set_available(factory, listing_id, False)
    assert price_alerts.check_gone_listings() == 1


def test_gone_check_ignores_listings_not_followed(factory, monkeypatch):
    monkeypatch.setattr(price_alerts.Notifier, "for_topic", lambda urls: pytest.fail("no alert"))
    with factory() as session:
        listing_id = _followed(session, price=80.0, target=None,
                               status=ListingStatus.SEEN.value).id
    _set_available(factory, listing_id, False)
    assert price_alerts.check_gone_listings() == 0
