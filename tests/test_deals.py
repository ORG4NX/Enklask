from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.deals import annotate_deals, deal_for
from app.enums import ListingStatus
from app.models import Base, Listing, ListingTopic, SavedSearch, Topic


def test_deal_window_around_the_median():
    assert deal_for(60.0, 100.0) == 40
    assert deal_for(61.0, 100.0) is None  # not cheap enough
    assert deal_for(20.0, 100.0) is None  # too cheap: accessory or part
    assert deal_for(None, 100.0) is None


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session, prices, hidden_prices=()):
    topic = Topic(name="T", slug="t")
    session.add(topic)
    session.flush()
    search = SavedSearch(topic_id=topic.id, name="player", query="gameboy player")
    session.add(search)
    session.flush()
    out = []
    rows = [(p, ListingStatus.SEEN) for p in prices] + [(p, ListingStatus.HIDDEN) for p in hidden_prices]
    for i, (price, status) in enumerate(rows):
        li = Listing(source="ebay", source_id=str(i), title=f"GB {i}", url=f"https://x/{i}",
                     price=price, status=status.value)
        session.add(li)
        session.flush()
        session.add(ListingTopic(listing_id=li.id, topic_id=topic.id, search_id=search.id))
        out.append(li)
    session.commit()
    return topic, out


def test_listing_well_under_the_median_gets_the_badge():
    session = _session()
    topic, listings = _seed(session, [100.0] * 8 + [60.0], hidden_prices=[5.0] * 20)
    annotate_deals(session, listings, topic.id)
    deal = listings[8].deal  # the 60 € one
    assert deal["pct"] == 40 and deal["median"] == 100.0 and deal["samples"] == 9
    assert deal["search"] == "player"
    assert listings[0].deal is None
    assert listings[-1].deal is None  # hidden, 5 €: under the floor


def test_no_badge_without_enough_prices():
    session = _session()
    topic, listings = _seed(session, [100.0] * 5 + [50.0])
    annotate_deals(session, listings, topic.id)
    assert all(li.deal is None for li in listings)
