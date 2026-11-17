from app.enums import Source
from app.sources import easycash
from app.sources.base import SearchQuery
from app.sources.easycash import EasyCashSource, parse_search_page


def _card(item_id: str, name: str, price: str, image: str = "url.jpg") -> str:
    """A trimmed-down result card, shaped like the live search page."""
    path = "/".join(item_id)
    data = (
        "{&quot;event&quot;:&quot;select_item&quot;,&quot;ecommerce&quot;:{&quot;items&quot;:"
        f"[{{&quot;item_id&quot;:&quot;{item_id}&quot;,&quot;item_name&quot;:&quot;{name}&quot;,"
        f"&quot;price&quot;:&quot;{price}&quot;,&quot;item_imparfait&quot;:&quot;0&quot;}}]}}}}"
    )
    return f'''
    <li
        class="clearfix block-link"
        data-href="https://bons-plans.easycash.fr/console-portable/item-{item_id}"
        data-layer-click="{data}"
        data-cypress-target="btn-item-search-list">
      <div class="image">
        <img src="https://xartdyafmb.cloudimg.io/https://cdn.easycash.fr/img/prod/{path}/src/{image}?w=137&amp;h=130&amp;ci_sign=abc" alt="">
      </div>
      <p class="infos-price-number">{price} €</p>
    </li>'''


PAGE = (
    "<ul>"
    + _card("001863904", "Console NINTENDO New 3DS Blanc", "139.99")
    + _card("001838024", "Console NINTENDO New 3DS Noir &amp; Or", "129.99", image="Capture.PNG")
    + _card("001863904", "Console NINTENDO New 3DS Blanc", "139.99")  # duplicate
    + "</ul>"
)


def test_parse_search_page_maps_cards():
    raws = parse_search_page(PAGE)

    assert [r.source_id for r in raws] == ["001863904", "001838024"]
    first, second = raws
    assert first.source is Source.EASYCASH
    assert first.title == "Console NINTENDO New 3DS Blanc"
    assert first.price == 139.99 and first.currency == "EUR"
    assert first.url == "https://bons-plans.easycash.fr/console-portable/item-001863904"
    # The original CDN image, not the 137px resizer URL.
    assert first.thumbnail == "https://cdn.easycash.fr/img/prod/0/0/1/8/6/3/9/0/4/src/url.jpg"
    assert first.photos == [first.thumbnail]
    assert second.title == "Console NINTENDO New 3DS Noir & Or"
    assert second.thumbnail.endswith("/src/Capture.PNG")


def test_parse_search_page_skips_broken_cards():
    broken = '<li class="clearfix block-link" data-href="https://x/1" data-layer-click="not json">'
    assert parse_search_page(broken) == []
    assert parse_search_page("<html>Aucun résultat</html>") == []


def test_search_sends_the_site_filters(monkeypatch):
    calls = []

    class _Resp:
        text = PAGE

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, **kwargs):
        calls.append((url, params))
        return _Resp()

    monkeypatch.setattr(easycash.httpx, "get", fake_get)
    raws = EasyCashSource().search(
        SearchQuery("new 3ds", price_min=100, price_max=199.5, condition="good")
    )

    assert len(raws) == 2
    url, params = calls[0]
    assert url == "https://bons-plans.easycash.fr/catalog/search"
    # Condition has no equivalent on the site and is left out.
    assert params == {"q": "new 3ds", "minPrice": "100", "maxPrice": "199.5"}


def test_search_drops_the_loose_matches(monkeypatch):
    class _Resp:
        text = PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(easycash.httpx, "get", lambda url, params=None, **kw: _Resp())
    assert EasyCashSource().search(SearchQuery("psone")) == []
    assert len(EasyCashSource().search(SearchQuery("3ds noir"))) == 1
