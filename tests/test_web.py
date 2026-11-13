from starlette.requests import Request



from app.enums import Source

from app.sources.base import RawListing

from app.web import routes as web_routes



templates = web_routes.templates





def test_listing_images_open_the_preview():

    for template_name in ("_card.html", "_search_results.html", "_watch_row.html"):

        source, _, _ = templates.env.loader.get_source(

            templates.env, template_name

        )

        assert 'onclick="openPreview(this)"' in source

        assert "cursor-zoom-in" in source





def test_preview_history_is_short_and_rejects_polluted_trails():

    history = [{"price": price} for price in range(10)]

    assert web_routes._preview_history(history) == history[-6:]

    assert web_routes._preview_history(history * 6) == []





class _Source:

    def __init__(self, name, *, results=None, error=None):

        self.name = name

        self._results = results or []

        self._error = error



    def search(self, query):

        if self._error:

            raise self._error

        return self._results





def test_live_search_displays_source_errors_without_hiding_results(monkeypatch):

    listing = RawListing(

        source=Source.EBAY,

        source_id="123",

        title="Nintendo Switch",

        url="https://example.com/123",

    )

    sources = [

        _Source(Source.EBAY, results=[listing]),

        _Source(Source.VINTED, error=RuntimeError("HTTP 403 challenge")),

    ]

    monkeypatch.setattr(web_routes, "get_enabled_sources", lambda: sources)

    request = Request({

        "type": "http",

        "method": "GET",

        "path": "/search/results",

        "query_string": b"q=switch",

        "headers": [],

    })



    response = web_routes.search_results_partial(request)

    html = response.body.decode()



    assert "Nintendo Switch" in html

    assert "Certaines sources" in html

    assert "pas pu être interrogées" in html

    assert "Vinted" in html

    assert "HTTP 403 challenge" in html





# ---------------------------------------------------------------------------

# Dashboard quick wins (filters in URL, Nouveau badge, French statuses,

# empty states, settings validation) — end-to-end over an in-memory database.

# ---------------------------------------------------------------------------



from datetime import datetime, timedelta



import pytest

from fastapi.testclient import TestClient

from sqlalchemy import create_engine

from sqlalchemy.orm import sessionmaker

from sqlalchemy.pool import StaticPool



from app.db import get_session

from app.main import app

from app.models import Base, Listing, ListingTopic, SavedSearch, Topic





@pytest.fixture

def db_session(monkeypatch):

    engine = create_engine(

        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool

    )

    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)



    def _override():

        session = factory()

        try:

            yield session

        finally:

            session.close()



    app.dependency_overrides[get_session] = _override

    monkeypatch.setattr(web_routes, "get_enabled_sources", lambda: [])

    session = factory()

    try:

        yield session

    finally:

        session.close()

        app.dependency_overrides.clear()





@pytest.fixture

def client(db_session):

    # No ``with``: the lifespan (real DB migration + scheduler) must not run.

    return TestClient(app)





def _topic_with_listing(session, *, last_viewed_at=None, with_search=True):

    topic = Topic(name="Switch", slug="switch", last_viewed_at=last_viewed_at)

    session.add(topic)

    session.flush()

    if with_search:

        session.add(SavedSearch(topic_id=topic.id, name="oled", query="switch oled", tags=["oled"]))

    listing = Listing(

        source="ebay", source_id="1", title="Switch OLED", url="https://x/1",

        price=100.0, first_seen=datetime(2026, 11, 13, 12, 0),

    )

    session.add(listing)

    session.flush()

    session.add(ListingTopic(listing_id=listing.id, topic_id=topic.id, tags=["oled"]))

    session.commit()

    return topic, listing





def test_topic_page_marks_new_badge_since_previous_visit(client, db_session):

    previous = datetime(2026, 11, 13, 11, 0)

    topic, listing = _topic_with_listing(db_session, last_viewed_at=previous)



    html = client.get("/t/switch").text

    assert "Nouveau" in html

    # The badge cursor is handed back to htmx requests, not written in the URL.

    assert f'"X-Since": "{previous.isoformat()}"' in html



    db_session.refresh(topic)

    assert topic.last_viewed_at > previous



    # A filter change over htmx keeps the badge (same cursor, grid only).

    partial = client.get(

        "/t/switch?sort=price_asc",

        headers={"HX-Request": "true", "X-Since": previous.isoformat()},

    ).text

    assert "Nouveau" in partial

    assert "<form" not in partial



    # A plain reload is a new visit: the listing is no longer new.

    assert "Nouveau" not in client.get("/t/switch").text





def test_first_visit_shows_no_badges(client, db_session):

    _topic_with_listing(db_session, last_viewed_at=None)

    assert "Nouveau" not in client.get("/t/switch").text





def test_topic_filters_are_a_form_that_pushes_its_url(client, db_session):

    _topic_with_listing(db_session)

    html = client.get("/t/switch?tag=oled").text

    assert 'hx-get="/t/switch"' in html

    assert 'hx-push-url="true"' in html

    assert 'name="tag" value="oled" class="sr-only peer" checked' in html

    # Statuses are labelled in French, not by their enum value.

    assert "Déjà vues" in html and ">Seen<" not in html





def test_topic_without_search_points_to_adding_one(client, db_session):

    topic = Topic(name="Vide", slug="vide")

    db_session.add(topic)

    db_session.commit()

    html = client.get("/t/vide").text

    assert "aucune recherche" in html

    assert 'href="/t/vide/settings"' in html

    assert "Aucune annonce trouvée" not in html





def test_topic_refresh_runs_in_background_and_reports_outcome(client, db_session, monkeypatch):

    import threading

    from app.jobs import jobs

    from app.poller import PollResult



    _topic_with_listing(db_session)

    jobs.clear()

    release = threading.Event()



    def fake_poll(topic_id):

        release.wait(5)

        return PollResult(new_count=2, errors={"leboncoin": "HTTP 403 DataDome"})



    monkeypatch.setattr(web_routes, "poll_topic", fake_poll)



    # POST answers immediately with the control in its running state.

    started = client.post("/refresh/switch")

    assert started.status_code == 200

    assert "Actualisation en cours" in started.text

    assert 'hx-get="/refresh/switch/status"' in started.text

    assert "HX-Trigger" not in started.headers



    # Still running: same fragment, no trigger yet.

    assert "Actualisation en cours" in client.get("/refresh/switch/status").text



    release.set()

    for _ in range(50):

        if not jobs.is_running("topic:" + str(db_session.query(Topic).one().id)):

            break

        threading.Event().wait(0.02)



    done = client.get("/refresh/switch/status")

    assert "2 nouvelles annonces" in done.text

    assert "Leboncoin en erreur" in done.text

    assert "HTTP 403 DataDome" in done.text  # full detail kept in the title

    trigger = done.headers["HX-Trigger"]

    assert "refresh-done" in trigger

    assert "toast" in trigger and "Leboncoin : HTTP 403 DataDome" in trigger



    # The topic page itself shows the last outcome and no longer polls.

    page = client.get("/t/switch").text

    assert "2 nouvelles annonces" in page

    assert 'hx-get="/refresh/switch/status"' not in page

    jobs.clear()





def test_topic_refresh_card_view_and_single_flight(client, db_session, monkeypatch):

    import threading

    from app.jobs import jobs



    _topic_with_listing(db_session)

    jobs.clear()

    release = threading.Event()

    calls = []



    def fake_poll(topic_id):

        calls.append(topic_id)

        release.wait(5)



    monkeypatch.setattr(web_routes, "poll_topic", fake_poll)



    first = client.post("/refresh/switch?view=card").text

    assert 'id="topic-card-' in first

    assert "animate-spin" in first

    assert 'hx-get="/refresh/switch/status?view=card"' in first



    client.post("/refresh/switch?view=card")  # hammering the button

    release.set()

    threading.Event().wait(0.1)

    assert calls == [db_session.query(Topic).one().id]  # one poll, not two

    jobs.clear()





def test_refresh_failure_is_reported_not_raised(client, db_session, monkeypatch):

    import threading

    from app.jobs import jobs



    _topic_with_listing(db_session)

    jobs.clear()



    def boom(topic_id):

        raise RuntimeError("browser crashed")



    monkeypatch.setattr(web_routes, "poll_topic", boom)

    client.post("/refresh/switch")

    threading.Event().wait(0.1)

    done = client.get("/refresh/switch/status")

    assert done.status_code == 200

    assert "Échec de l&#39;actualisation" in done.text  # Jinja escapes the apostrophe

    assert "browser crashed" in done.headers["HX-Trigger"]

    jobs.clear()





def test_home_has_global_refresh_and_last_poll_age(client, db_session, monkeypatch):

    import threading

    from app.jobs import jobs



    topic, _ = _topic_with_listing(db_session)

    topic.last_polled_at = datetime(2026, 11, 13, 12, 0)

    db_session.commit()

    jobs.clear()

    gate = threading.Event()

    monkeypatch.setattr(web_routes, "poll_once", lambda: gate.wait(5))



    html = client.get("/").text

    assert "Tout actualiser" in html

    assert "Actualisé" in html and "il y a" in html

    assert 'hx-trigger="refresh-done from:body"' in html



    started = client.post("/refresh").text

    assert "Actualisation en cours" in started

    assert 'hx-get="/refresh/status"' in started

    gate.set()

    threading.Event().wait(0.1)

    assert "Actualisation en cours" not in client.get("/refresh/status").text

    jobs.clear()





def test_search_refresh_runs_in_background(client, db_session, monkeypatch):

    import threading

    from app.jobs import jobs

    from app.poller import PollResult



    topic, _ = _topic_with_listing(db_session)

    search = db_session.query(SavedSearch).one()

    jobs.clear()

    monkeypatch.setattr(web_routes, "poll_search", lambda sid: PollResult(new_count=1))



    row = client.post(f"/t/switch/searches/{search.id}/refresh").text

    assert f'id="search-row-{search.id}"' in row

    threading.Event().wait(0.1)

    done = client.get(f"/t/switch/searches/{search.id}/refresh/status")

    assert "1 nouvelle annonce" in done.text

    assert "refresh-done" in done.headers["HX-Trigger"]

    jobs.clear()





def test_settings_save_redirects_and_rejects_bad_numbers(client, monkeypatch):

    saved: dict = {}

    monkeypatch.setattr(web_routes, "save_settings", lambda u: saved.update(u))

    monkeypatch.setattr(web_routes, "reschedule_poll", lambda _m: None)



    bad = client.post(

        "/settings",

        data={"poll_interval_minutes": "abc", "home_lat": "123", "home_lon": "2,5"},

        follow_redirects=False,

    )

    assert bad.status_code == 400

    assert "nombre entier" in bad.text

    assert "comprise entre -90 et 90" in bad.text

    assert 'value="abc"' in bad.text  # what the user typed is kept

    assert not saved



    ok = client.post(

        "/settings",

        data={"poll_interval_minutes": "15", "home_lat": "48.1", "home_lon": "-1,6"},

        follow_redirects=False,

    )

    assert ok.status_code == 303

    assert ok.headers["location"] == "/settings?saved=1"

    assert saved["poll_interval_minutes"] == 15

    assert saved["home_lon"] == -1.6



    assert "Configuration enregistrée" in client.get("/settings?saved=1").text





def test_icon_buttons_have_accessible_names():

    for name in ("_card.html", "_topic_card.html", "_search_row.html", "_watch_row.html", "base.html"):

        source, _, _ = templates.env.loader.get_source(templates.env, name)

        assert "aria-label=" in source, name

