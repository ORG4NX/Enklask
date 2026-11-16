from app.enums import Source
from app.sources import label_emmaus
from app.sources.base import SearchQuery
from app.sources.label_emmaus import LabelEmmausSource, parse_catalogue_page

BASE = "https://www.label-emmaus.co"


def _card(offer_id: str, title: str, price: str, merchant: str) -> str:
    """A trimmed-down product card, shaped like the live catalogue page."""
    return f'''
    <div class="product-card catalogue-card"
         data-id="{offer_id}"
         data-ga4-item="{{&quot;item_id&quot;: &quot;{offer_id}&quot;, &quot;price&quot;: {price}, &quot;item_merchant&quot;: &quot;{merchant}&quot;}}" data-ga4-source="listing produit"
         style="order: 1">
      <div class="product-image-wrapper">
        <a href="/fr/item-{offer_id}/"
           title="{title}"
           data-offer_id="{offer_id}"
           class="js_gtmonclick max-w-full">
          <img class="image-index-0 solo-img"
               alt="{title} - Photo 0"
               src="/media/ext/500x500/cdn.example/images/{offer_id}.jpeg.webp" width="250">
        </a>
      </div>
      <a href="/fr/item-{offer_id}/" title="{title}" class="product-title">{title}</a>
    </div>'''


PAGE = (
    _card("113207453", "Console Nintendo DS +2 jeux", "55.0", "AMISEP")
    + _card("108166943", "Jeu Game Boy Advance &quot;Age de glace 2&quot;", "7.0", "Emma&#252;s Lyon")
    + _card("113207453", "Console Nintendo DS +2 jeux", "55.0", "AMISEP")  # duplicate
)


def test_parse_catalogue_page_maps_cards():
    raws = parse_catalogue_page(PAGE, BASE)

    assert [r.source_id for r in raws] == ["113207453", "108166943"]
    first, second = raws
    assert first.source is Source.LABEL_EMMAUS
    assert first.title == "Console Nintendo DS +2 jeux"
    assert first.price == 55.0 and first.currency == "EUR"
    assert first.url == f"{BASE}/fr/item-113207453/"
    assert first.thumbnail == f"{BASE}/media/ext/500x500/cdn.example/images/113207453.jpeg.webp"
    assert first.description == "Vendu par AMISEP"
    assert second.title == 'Jeu Game Boy Advance "Age de glace 2"'
    assert second.description == "Vendu par Emmaüs Lyon"


def test_parse_catalogue_page_tolerates_missing_data():
    no_link = '<div class="product-card catalogue-card" data-id="1" data-ga4-item="{}"><p>rien</p></div>'
    assert parse_catalogue_page(no_link, BASE) == []
    bad_json = (
        '<div class="product-card catalogue-card" data-id="2" data-ga4-item="oops">'
        '<a href="/fr/x-2/" title="Objet"></a></div>'
    )
    (raw,) = parse_catalogue_page(bad_json, BASE)
    assert (raw.source_id, raw.title, raw.price, raw.thumbnail) == ("2", "Objet", None, None)


def test_search_sends_the_site_filters(monkeypatch):
    calls = []

    class _Resp:
        text = PAGE

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, **kwargs):
        calls.append((url, params))
        return _Resp()

    monkeypatch.setattr(label_emmaus.httpx, "get", fake_get)
    source = LabelEmmausSource()
    assert len(source.search(SearchQuery("nintendo", price_max=60, condition="like_new"))) == 2
    url, params = calls[0]
    assert url == f"{BASE}/fr/catalogue/"
    assert params == {"q": "nintendo", "max_prix": "60", "tri": "recent", "etat": "très bon état"}

    # "for_parts" has no equivalent on the site: no condition filter is sent.
    source.search(SearchQuery("nintendo", condition="for_parts", sort="relevance"))
    assert calls[1][1] == {"q": "nintendo"}
