from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text

from app import migrate
from app.enums import Source
from app.filters import drop_excluded, excluded_by, normalize, parse_exclude_words
from app.poller import _poll_one
from app.sources.base import RawListing


def test_normalize_folds_case_accents_and_punctuation():
    assert normalize("Écran  Wii-U!") == "ecran wii u"


def test_parse_exclude_words_trims_dedupes_and_keeps_phrases():
    assert parse_exclude_words(" Wii, gamecube ,, wii , boîte vide") == [
        "Wii", "gamecube", "boîte vide",
    ]
    assert len(parse_exclude_words(",".join(f"w{i}" for i in range(50)))) == 30


def test_excluded_by_matches_whole_words_only():
    words = ["wii", "boite vide", "pièces"]
    assert excluded_by("Console Nintendo WII + Wii Fit", words) == "wii"
    assert excluded_by("Game Boy Player - BOÎTE VIDE", words) == "boite vide"
    assert excluded_by("Lot de pieces détachées", words) == "pièces"
    assert excluded_by("Nintendo WiiU Basic", words) is None  # not a whole word
    assert excluded_by("Game Boy Player", words) is None
    assert excluded_by("anything", []) is None


def _raw(item_id, title):
    return RawListing(source=Source.EBAY, source_id=str(item_id), title=title, url="u")


def test_drop_excluded_keeps_the_rest_in_order():
    raws = [_raw(1, "Game Boy Player"), _raw(2, "Wii lot"), _raw(3, "GBP + disque")]
    assert [r.source_id for r in drop_excluded(raws, ["wii"])] == ["1", "3"]
    assert drop_excluded(raws, None) == raws


class _Source:
    name = Source.EBAY

    def search(self, query):
        return [_raw(1, "Game Boy Player"), _raw(2, "Console Wii + jeux")]


def test_poller_skips_excluded_results_and_leaves_them_out_of_the_seen_set(monkeypatch):
    monkeypatch.setattr("app.poller.time.sleep", lambda _: None)
    stored = []

    class _Scope:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.poller.session_scope", lambda: _Scope())
    monkeypatch.setattr(
        "app.poller.upsert_listing", lambda session, raw, **kw: stored.append(raw.source_id)
    )
    topic = SimpleNamespace(id=1, name="Nintendo")
    search = SimpleNamespace(
        id=2, query="game boy player", price_min=None, price_max=None,
        max_distance_km=None, condition=None, sources=[], tags=[],
        exclude_words=["wii"],
    )
    _, _, _, _, source_ids, _, _ = _poll_one(topic, search, [_Source()])
    assert stored == ["1"]
    # The Wii ad counts as missing, so a copy stored earlier fades out.
    assert source_ids == {"ebay": {"1"}}


def test_migration_adds_the_column_to_an_existing_table(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE saved_searches (id INTEGER PRIMARY KEY, price_min FLOAT, "
            "condition VARCHAR(16))"
        ))
        conn.execute(text("INSERT INTO saved_searches (id) VALUES (1)"))
    monkeypatch.setattr(migrate, "engine", engine)
    migrate._ensure_saved_search_columns()
    migrate._ensure_saved_search_columns()  # idempotent
    columns = {c["name"] for c in inspect(engine).get_columns("saved_searches")}
    assert "exclude_words" in columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT exclude_words FROM saved_searches")).scalar() == "[]"


def test_drop_out_of_price_keeps_budget_and_unknown_prices():
    from app.filters import drop_out_of_price

    def raw(sid, price):
        return RawListing(source=Source.EASYCASH, source_id=sid, title=sid,
                          url="u", price=price)

    raws = [raw("low", 10.0), raw("in", 80.0), raw("high", 200.0), raw("none", None)]
    kept = [r.source_id for r in drop_out_of_price(raws, 50, 150)]
    assert kept == ["in", "none"]
    assert [r.source_id for r in drop_out_of_price(raws, None, 100)] == ["low", "in", "none"]
    assert drop_out_of_price(raws, None, None) == raws


def test_live_search_drops_excluded_and_out_of_price(monkeypatch):
    from fastapi.testclient import TestClient

    import app.web.routes as routes
    from app.main import app as fastapi_app

    class Fake:
        name = Source.EASYCASH

        def search(self, query):
            return [
                RawListing(source=Source.EASYCASH, source_id=s, title=t, url="u", price=p)
                for s, t, p in [
                    ("1", "Game Boy Player", 90.0),
                    ("2", "Console Wii blanche", 60.0),
                    ("3", "Game Boy Player + disque", 400.0),
                ]
            ]

    monkeypatch.setattr(routes, "get_enabled_sources", lambda: [Fake()])
    html = TestClient(fastapi_app).get(
        "/search/results",
        params={"q": "gameboy player", "price_max": "150", "exclude": "wii"},
    ).text
    assert "Game Boy Player" in html
    assert "Wii blanche" not in html
    assert "+ disque" not in html
