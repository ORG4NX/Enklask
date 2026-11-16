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
