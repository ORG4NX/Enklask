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

from sqlalchemy import create_engine, select

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



def test_hiding_a_listing_offers_an_undo_that_restores_it(client, db_session):
    import json as _json

    _, listing = _topic_with_listing(db_session)
    listing.status = "seen"
    db_session.commit()
    since = "2026-11-13T11:00:00"

    resp = client.post(
        f"/partials/listings/{listing.id}/status",
        data={"value": "hidden", "since": since},
    )
    assert resp.status_code == 200
    # An invisible placeholder keeps the card's spot in the grid.
    assert f'id="card-{listing.id}"' in resp.text and 'class="hidden"' in resp.text
    toast = _json.loads(resp.headers["HX-Trigger"])["toast"]
    assert toast["message"] == "Annonce masquée"
    action = toast["action"]
    assert action["url"] == f"/partials/listings/{listing.id}/restore"
    assert action["target"] == f"#card-{listing.id}"
    assert action["values"] == {"value": "seen", "since": since}
    db_session.refresh(listing)
    assert listing.status == "hidden"

    resp = client.post(action["url"], data=action["values"])
    assert resp.status_code == 200
    assert f'id="card-{listing.id}"' in resp.text
    # The "Nouveau" cursor travelled with the undo, so the badge survives.
    assert "Nouveau" in resp.text
    db_session.refresh(listing)
    assert listing.status == "seen"


def test_restore_never_sets_hidden_and_leaves_visible_listings_alone(client, db_session):
    _, listing = _topic_with_listing(db_session)
    listing.status = "hidden"
    db_session.commit()

    client.post(f"/partials/listings/{listing.id}/restore", data={"value": "hidden"})
    db_session.refresh(listing)
    assert listing.status == "seen"

    listing.status = "liked"
    db_session.commit()
    client.post(f"/partials/listings/{listing.id}/restore", data={"value": "new"})
    db_session.refresh(listing)
    assert listing.status == "liked"


def test_images_are_upgraded_to_larger_cdn_variants():
    f = web_routes._img_size
    assert f("https://i.ebayimg.com/images/g/abc/s-l225.jpg").endswith("/s-l960.jpg")
    assert f("https://i.ebayimg.com/images/g/abc/s-l500.webp", "full").endswith("/s-l1600.webp")
    assert f("https://img.leboncoin.fr/api/v1/i/x.jpg?rule=ad-thumb").endswith("?rule=ad-image")
    assert f("https://img.leboncoin.fr/api/v1/i/x.jpg?rule=ad-image", "full").endswith("?rule=ad-large")
    vinted = "https://images1.vinted.net/t/a/f800/1.jpeg?s=e"
    assert f(vinted) == vinted
    assert f(None) is None


def test_grid_card_shows_the_first_photo_over_the_thumbnail(client, db_session):
    topic, listing = _topic_with_listing(db_session)
    listing.thumbnail = "https://images1.vinted.net/t/a/f800/1.jpeg"
    listing.photos = ["https://images1.vinted.net/tc/a/1.jpeg", "https://images1.vinted.net/tc/b/2.jpeg"]
    other = Listing(
        source="leboncoin", source_id="2", title="Switch Lite", url="https://x/2",
        thumbnail="https://img.leboncoin.fr/api/v1/i/x.jpg?rule=ad-thumb", photos=[],
        first_seen=datetime(2026, 11, 13, 12, 0),
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(ListingTopic(listing_id=other.id, topic_id=topic.id, tags=[]))
    db_session.commit()

    html = client.get(f"/t/{topic.slug}").text
    assert '<img src="https://images1.vinted.net/tc/a/1.jpeg"' in html
    assert '<img src="https://images1.vinted.net/t/a/f800/1.jpeg"' not in html
    # No photos: the thumbnail is the fallback, upgraded to a larger variant.
    assert '<img src="https://img.leboncoin.fr/api/v1/i/x.jpg?rule=ad-image"' in html


def _search_of(session, topic):
    return session.scalar(select(SavedSearch).where(SavedSearch.topic_id == topic.id))


def test_search_edit_form_is_prefilled_and_saves_in_place(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)
    search.price_max = 250.0
    db_session.commit()

    form = client.get(f"/t/{topic.slug}/searches/{search.id}/edit")
    assert form.status_code == 200
    assert f'id="search-row-{search.id}"' in form.text
    assert 'value="switch oled"' in form.text and 'value="250"' in form.text

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={
            "query": "switch lite", "name": "", "tags": "lite, bleu",
            "price_min": "20", "price_max": "99,5", "condition": "good",
            "sources": ["vinted", "leboncoin", "bogus"],
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # The read-only row comes back, showing the new values.
    assert "<form" not in resp.text.split(">", 1)[0]
    assert "switch lite" in resp.text and "Vinted, Leboncoin seulement" in resp.text
    db_session.refresh(search)
    assert (search.query, search.name, search.tags) == ("switch lite", "switch lite", ["lite", "bleu"])
    assert (search.price_min, search.price_max, search.condition) == (20.0, 99.5, "good")
    assert search.sources == ["vinted", "leboncoin"]


def test_search_edit_errors_keep_the_form_and_the_typed_values(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={"query": "switch", "price_min": "100", "price_max": "abc"},
    )
    assert resp.status_code == 200
    assert "Doit être un nombre." in resp.text
    assert 'value="abc"' in resp.text and 'aria-invalid="true"' in resp.text

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={"query": "", "price_min": "100", "price_max": "50"},
    )
    assert "Les mots-clés sont obligatoires." in resp.text
    assert "Doit être supérieur au prix min." in resp.text
    db_session.refresh(search)
    assert search.query == "switch oled" and search.price_min is None


def test_search_edit_cancel_returns_the_row(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)
    resp = client.get(f"/t/{topic.slug}/searches/{search.id}/row")
    assert resp.status_code == 200
    assert f'id="search-row-{search.id}"' in resp.text and "Modifier la recherche" in resp.text


def test_search_of_another_topic_cannot_be_edited(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)
    other = Topic(name="Autre", slug="autre")
    db_session.add(other)
    db_session.commit()
    assert client.get(f"/t/autre/searches/{search.id}/edit").status_code == 404
    assert client.post(f"/t/autre/searches/{search.id}", data={"query": "x"}).status_code == 404


def test_search_toggle_and_delete_answer_htmx_in_place(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)
    hx = {"HX-Request": "true"}

    resp = client.post(f"/t/{topic.slug}/searches/{search.id}/toggle", headers=hx)
    assert resp.status_code == 200 and f'id="search-row-{search.id}"' in resp.text
    assert 'aria-checked="false"' in resp.text
    db_session.refresh(search)
    assert search.enabled is False

    search_id = search.id
    resp = client.post(f"/t/{topic.slug}/searches/{search_id}/delete", headers=hx)
    assert resp.status_code == 200 and resp.text == ""
    db_session.expunge_all()
    assert db_session.get(SavedSearch, search_id) is None


def test_add_search_accepts_sources(client, db_session):
    topic, _ = _topic_with_listing(db_session, with_search=False)
    client.post(
        f"/t/{topic.slug}/searches",
        data={"query": "zelda", "sources": ["ebay"], "price_max": "oops"},
    )
    search = _search_of(db_session, topic)
    assert (search.query, search.sources, search.price_max) == ("zelda", ["ebay"], None)


def test_topic_settings_page_renders_edit_controls_and_source_choice(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    html = client.get(f"/t/{topic.slug}/settings").text
    assert "Modifier la recherche oled" in html
    assert 'name="sources" value="leboncoin"' in html
    assert "1 active sur 1" in html


def test_topic_title_switches_to_the_other_topics(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    html = client.get(f"/t/{topic.slug}").text
    assert '<details class="topic-switcher' not in html  # a lone topic keeps a plain title

    db_session.add(Topic(name="Zelda", slug="zelda"))
    db_session.commit()
    html = client.get(f"/t/{topic.slug}").text
    assert '<details class="topic-switcher' in html
    assert 'href="/t/zelda"' in html
    assert 'href="/t/switch" class="block' not in html  # the current one is not listed


def test_city_lookup_lists_places_with_their_coordinates(client, monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"display_name": "Rennes, Ille-et-Vilaine, France", "lat": "48.1113387", "lon": "-1.6800198"},
                {"display_name": "broken", "lat": None, "lon": "x"},
            ]

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        return _Resp()

    monkeypatch.setattr(web_routes.httpx, "get", fake_get)
    html = client.get("/partials/geocode", params={"q": " Rennes "}).text
    assert 'data-lat="48.1113" data-lon="-1.68"' in html
    assert "Rennes, Ille-et-Vilaine, France" in html and "broken" not in html
    url, params, headers = calls[0]
    assert "nominatim" in url and params["q"] == "Rennes" and params["limit"] == 5
    assert headers["User-Agent"].startswith("Enklask")


def test_city_lookup_reports_short_queries_and_failures(client, monkeypatch):
    assert "au moins deux lettres" in client.get("/partials/geocode", params={"q": "R"}).text

    def boom(*args, **kwargs):
        raise web_routes.httpx.ConnectError("offline")

    monkeypatch.setattr(web_routes.httpx, "get", boom)
    html = client.get("/partials/geocode", params={"q": "Rennes"}).text
    assert "indisponible" in html


def test_shipping_and_tag_filters_apply_before_pagination(db_session):
    from app.api.routes import _query_listings

    topic = Topic(name="Consoles", slug="consoles")
    db_session.add(topic)
    db_session.flush()
    # The 4 newest listings match nothing; only the 2 oldest match both filters.
    for i in range(6):
        match = i < 2
        listing = Listing(
            source="vinted", source_id=f"s{i}", title=f"Item {i}", url=f"https://x/{i}",
            first_seen=datetime(2026, 11, 1) + timedelta(days=i),
            shipping_options=["mondial_relay"] if match else ["hand_delivery"],
        )
        db_session.add(listing)
        db_session.flush()
        db_session.add(ListingTopic(
            listing_id=listing.id, topic_id=topic.id, tags=["oled", "promo"] if match else ["lite"],
        ))
    db_session.commit()

    def titles(**kw):
        rows = _query_listings(db_session, topic_id=topic.id, limit=2, offset=0, **kw)
        return [r.title for r in rows]

    assert titles(shipping="mondial_relay") == ["Item 1", "Item 0"]
    assert titles(tags=["promo"]) == ["Item 1", "Item 0"]
    assert titles(tags=["oled", "lite"]) == ["Item 5", "Item 4"]  # any tag matches
    assert titles(shipping="mondial_relay", tags=["lite"]) == []
    assert len(_query_listings(db_session, topic_id=topic.id, tags=["lite"], limit=10, offset=2)) == 2


def test_new_sources_have_labels_and_settings_toggles(client, db_session):
    assert web_routes.SOURCE_LABELS["easycash"] == "Easy Cash"
    html = client.get("/settings").text
    assert 'name="enable_easycash"' in html and 'name="enable_label_emmaus"' in html

    topic, listing = _topic_with_listing(db_session)
    listing.source = "label_emmaus"
    db_session.commit()
    html = client.get(f"/t/{topic.slug}").text
    assert "Label Emmaüs" in html and "bg-emerald-50 text-emerald-600" in html


def test_search_edit_saves_and_shows_the_leboncoin_radius(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={"query": "canapé", "max_distance_km": "30"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200 and "≤ 30 km" in resp.text
    db_session.refresh(search)
    assert search.max_distance_km == 30.0

    form = client.get(f"/t/{topic.slug}/searches/{search.id}/edit")
    assert 'name="max_distance_km" value="30"' in form.text

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={"query": "canapé", "max_distance_km": "loin"},
    )
    assert "Doit être un nombre." in resp.text


def test_search_edit_saves_and_shows_required_words(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = _search_of(db_session, topic)

    resp = client.post(
        f"/t/{topic.slug}/searches/{search.id}",
        data={"query": "game boy player", "required_words": "player, Player ,gba"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200 and "avec : player, gba" in resp.text
    db_session.refresh(search)
    assert search.required_words == ["player", "gba"]
    form = client.get(f"/t/{topic.slug}/searches/{search.id}/edit")
    assert 'value="player, gba"' in form.text


def test_watch_row_sets_shows_and_clears_a_target_price(client, db_session):
    _, listing = _topic_with_listing(db_session)
    listing.status = "liked"
    listing.price = 80.0
    db_session.commit()

    resp = client.post(f"/partials/listings/{listing.id}/target", data={"target_price": "50"})
    assert resp.status_code == 200
    assert 'name="target_price"' in resp.text and 'value="50"' in resp.text
    assert "encore 30" in resp.text
    db_session.refresh(listing)
    assert listing.target_price == 50.0 and listing.target_alerted is False

    # A target at or above the current price shows as reached, without an alert.
    resp = client.post(f"/partials/listings/{listing.id}/target", data={"target_price": "90,5"})
    assert "Cible atteinte" in resp.text
    db_session.refresh(listing)
    assert listing.target_price == 90.5 and listing.target_alerted is True

    client.post(f"/partials/listings/{listing.id}/target", data={"target_price": ""})
    db_session.refresh(listing)
    assert listing.target_price is None

    page = client.get("/liked")
    assert "Alerte à" in page.text


def test_home_lights_show_what_each_source_answered(client, db_session, monkeypatch):
    from types import SimpleNamespace

    from app import health

    _topic_with_listing(db_session)

    class Src:
        def __init__(self, name):
            self.name = SimpleNamespace(value=name)

    monkeypatch.setattr(
        web_routes, "get_enabled_sources",
        lambda: [Src("ebay"), Src("vinted"), Src("easycash")],
    )
    health.record("ebay", error=RuntimeError("eBay challenge page (bot check) on www.ebay.fr"))
    health.record("vinted", count=20)
    health.record("easycash", count=30, filters_fixed=17)

    html = client.get("/").text
    assert "· bloqué" in html and "Bloqué par une protection anti-bot" in html
    assert "Répond normalement" in html
    assert "Filtre prix ignoré par le site : 17" in html


def _search_with_hidden(session):
    from app.enums import ListingStatus
    topic = Topic(name="GB", slug="gb")
    session.add(topic)
    session.flush()
    search = SavedSearch(topic_id=topic.id, name="player", query="gameboy player")
    session.add(search)
    session.flush()
    titles = [
        ("Console Wii blanche", ListingStatus.HIDDEN), ("Wii + manettes", ListingStatus.HIDDEN),
        ("Lot Wii Sports", ListingStatus.HIDDEN), ("GameCube violette", ListingStatus.HIDDEN),
        ("Game Boy Player GameCube", ListingStatus.SEEN), ("GameBoy Player + disque", ListingStatus.NEW),
    ]
    for i, (title, status) in enumerate(titles):
        listing = Listing(source="ebay", source_id=str(i), title=title, url=f"https://x/{i}",
                          price=50.0, status=status.value)
        session.add(listing)
        session.flush()
        session.add(ListingTopic(listing_id=listing.id, topic_id=topic.id, search_id=search.id))
    session.commit()
    return topic, search


def test_settings_suggest_words_from_hidden_listings(client, db_session):
    topic, search = _search_with_hidden(db_session)
    html = client.get("/t/gb/settings").text
    assert "Exclure ?" in html
    assert "+ wii" in html
    assert "1 mot à exclure ?" in client.get("/t/gb").text


def test_accepting_a_suggestion_adds_the_excluded_word(client, db_session):
    topic, search = _search_with_hidden(db_session)
    r = client.post(f"/t/gb/searches/{search.id}/suggestions/accept", data={"word": "wii"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "sans : wii" in r.text
    assert "+ wii" not in r.text
    db_session.expire_all()
    assert db_session.get(SavedSearch, search.id).exclude_words == ["wii"]


def test_dismissed_suggestion_is_not_offered_again(client, db_session):
    topic, search = _search_with_hidden(db_session)
    r = client.post(f"/t/gb/searches/{search.id}/suggestions/dismiss", data={"word": "wii"},
                    headers={"HX-Request": "true"})
    assert "+ wii" not in r.text
    db_session.expire_all()
    assert db_session.get(SavedSearch, search.id).ignored_suggestions == ["wii"]
    assert "+ wii" not in client.get("/t/gb/settings").text


def test_accepting_a_suggestion_hides_matching_listings_with_undo(client, db_session):
    import json as _json
    from app.enums import ListingStatus
    topic, search = _search_with_hidden(db_session)
    extra = Listing(source="ebay", source_id="w", title="Wii Fit", url="https://x/w",
                    price=10.0, status=ListingStatus.NEW.value)
    liked = Listing(source="ebay", source_id="l", title="Wii collector", url="https://x/l",
                    price=10.0, status=ListingStatus.LIKED.value)
    db_session.add_all([extra, liked])
    db_session.flush()
    for li in (extra, liked):
        db_session.add(ListingTopic(listing_id=li.id, topic_id=topic.id, search_id=search.id))
    db_session.commit()

    r = client.post(f"/t/gb/searches/{search.id}/suggestions/accept", data={"word": "wii"},
                    headers={"HX-Request": "true"})
    toast = _json.loads(r.headers["HX-Trigger"])["toast"]
    assert "1 annonce masquée" in toast["message"]
    db_session.expire_all()
    assert db_session.get(Listing, extra.id).status == "hidden"
    assert db_session.get(Listing, liked.id).status == "liked"  # followed: untouched

    action = toast["action"]
    client.post(action["url"], data=action["values"])
    db_session.expire_all()
    assert db_session.get(Listing, extra.id).status == "new"
    assert db_session.get(SavedSearch, search.id).exclude_words == ["wii"]


def test_gone_followed_listing_stays_on_the_watch_page(client, db_session):
    from app.enums import ListingStatus
    topic, listing = _topic_with_listing(db_session)
    listing.status = ListingStatus.LIKED.value
    for link in listing.topic_links:
        link.is_available = False
    db_session.commit()
    html = client.get("/liked").text
    assert "Switch OLED" in html
    assert "Plus en ligne" in html
    assert "1 hors ligne" in html


def test_topic_grid_shows_the_deal_badge(client, db_session):
    topic = Topic(name="Deals", slug="deals")
    db_session.add(topic)
    db_session.flush()
    search = SavedSearch(topic_id=topic.id, name="player", query="gameboy player")
    db_session.add(search)
    db_session.flush()
    for i, price in enumerate([100.0] * 8 + [60.0]):
        li = Listing(source="ebay", source_id=f"d{i}", title=f"GB {i}", url=f"https://x/d{i}",
                     price=price)
        db_session.add(li)
        db_session.flush()
        db_session.add(ListingTopic(listing_id=li.id, topic_id=topic.id, search_id=search.id))
    db_session.commit()
    html = client.get("/t/deals").text
    assert html.count("🔥 −40 %") == 1


def test_search_edit_saves_the_deals_only_notification(client, db_session):
    topic, _ = _topic_with_listing(db_session)
    search = db_session.query(SavedSearch).one()
    form = client.get(f"/t/switch/searches/{search.id}/edit").text
    assert 'name="notify_deals_only"' in form
    r = client.post(f"/t/switch/searches/{search.id}",
                    data={"query": "switch oled", "notify_deals_only": "1"})
    assert "bonnes affaires seulement" in r.text
    db_session.expire_all()
    assert db_session.get(SavedSearch, search.id).notify_deals_only is True
    client.post(f"/t/switch/searches/{search.id}", data={"query": "switch oled"})
    db_session.expire_all()
    assert db_session.get(SavedSearch, search.id).notify_deals_only is False
