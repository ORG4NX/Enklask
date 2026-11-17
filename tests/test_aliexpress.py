import json

import pytest

from app.enums import Source
from app.sources import aliexpress
from app.sources.aliexpress import AliExpressBlocked, AliExpressSource, parse_search_page
from app.sources.base import SearchQuery


def _item(pid, title, price, tags=(), sold="464 vendus"):
    return {
        "productId": pid,
        "title": {"displayTitle": title},
        "prices": {"salePrice": {"currencyCode": "EUR", "minPrice": price}},
        "image": {"imgUrl": f"//ae-pic-a1.aliexpress-media.com/kf/{pid}.jpg"},
        "evaluation": {"starRating": 4.7},
        "trade": {"tradeDesc": sold},
        "sellingPoints": [{"tagContent": {"tagText": t}} for t in tags],
    }


ITEMS = [
    _item("1005001", "Kit écran IPS V5 pour GBA", 52.39,
          tags=["Livraison gratuite", "Prix le plus bas des 30 derniers jours: 57,95€"]),
    _item("1005002", "Coque de remplacement GBA SP", 8.5, tags=["Livraison: 2,99€"]),
    _item("1005003", "Imprimante 3D ELEGOO Saturn 4", 299.0),
    _item("1005001", "Kit écran IPS V5 pour GBA", 52.39),  # duplicate
    {"productId": "", "title": {"displayTitle": "broken"}},
]
PAGE = ('<html><script>window._dida_config_._init_data_={data:{"root":{"fields":{'
        '"mods":{"itemList":{"content":' + json.dumps(ITEMS) + '}}}}}}</script></html>')


def test_parse_search_page_maps_the_cards():
    raws = parse_search_page(PAGE)
    assert [r.source_id for r in raws] == ["1005001", "1005002", "1005003"]
    first = raws[0]
    assert first.source == Source.ALIEXPRESS
    assert first.url == "https://fr.aliexpress.com/item/1005001.html"
    assert first.price == 52.39 and first.currency == "EUR"
    assert first.thumbnail == "https://ae-pic-a1.aliexpress-media.com/kf/1005001.jpg"
    assert first.shipping_cost == 0.0
    assert "4.7★" in first.description and "464 vendus" in first.description
    assert "30 derniers jours" in first.description
    assert raws[1].shipping_cost == 2.99
    assert raws[2].shipping_cost is None


def test_parse_search_page_without_items():
    assert parse_search_page("<html>rien</html>") == []


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def source(monkeypatch):
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append((url, params))
        return source.response

    browser_calls = []

    def fake_browser(base, profile, url):
        browser_calls.append(url)
        if source.browser_page is None:
            raise aliexpress.AliExpressBrowserError("captcha in the browser too")
        return source.browser_page

    monkeypatch.setattr(aliexpress.cffi_requests, "get", fake_get)
    monkeypatch.setattr(aliexpress, "fetch_aliexpress_html", fake_browser)
    aliexpress.reset_cooldown()
    source.calls = calls
    source.browser_calls = browser_calls
    source.response = _Resp(PAGE)
    source.browser_page = None  # None = the browser gets the captcha too
    yield source
    aliexpress.reset_cooldown()


def test_search_sends_filters_and_keeps_on_topic_titles(source):
    raws = AliExpressSource().search(
        SearchQuery("gba ips", price_min=5, price_max=60, sort="price_asc")
    )
    assert [r.source_id for r in raws] == ["1005001"]
    url, params = source.calls[0]
    assert url == "https://fr.aliexpress.com/w/wholesale-gba-ips.html"
    assert params == {"SearchText": "gba ips", "minPrice": "5", "maxPrice": "60",
                      "SortType": "price_asc"}


def test_recent_sort_stays_on_best_match(source):
    AliExpressSource().search(SearchQuery("gba"))
    assert "SortType" not in source.calls[0][1]


def test_second_hand_condition_returns_nothing(source):
    assert AliExpressSource().search(SearchQuery("gba", condition="good")) == []
    assert source.calls == []


def test_captcha_page_raises_blocked_when_the_browser_fails_too(source):
    source.response = _Resp("<html>punish captcha slide to verify</html>")
    with pytest.raises(AliExpressBlocked, match="navigateur de secours"):
        AliExpressSource().search(SearchQuery("gba"))
    aliexpress.reset_cooldown()
    source.response = _Resp("", status=429)
    with pytest.raises(AliExpressBlocked):
        AliExpressSource().search(SearchQuery("gba"))


def test_captcha_falls_back_to_the_browser_then_keeps_using_it(source):
    source.response = _Resp("<html>punish</html>")
    source.browser_page = PAGE
    raws = AliExpressSource().search(SearchQuery("gba ips", price_max=60))
    assert [r.source_id for r in raws] == ["1005001"]
    assert source.browser_calls == [
        "https://fr.aliexpress.com/w/wholesale-gba-ips.html?SearchText=gba+ips&maxPrice=60"
    ]
    # The plain request is left alone for a while: straight to the browser.
    AliExpressSource().search(SearchQuery("gba ips"))
    assert len(source.calls) == 1 and len(source.browser_calls) == 2


def test_aliexpress_is_only_queried_when_ticked():
    from app.sources.base import wanted_sources

    class _S:
        def __init__(self, name, opt_in=False):
            self.name = Source(name)
            self.opt_in = opt_in

    ebay, ali = _S("ebay"), AliExpressSource.__new__(AliExpressSource)
    assert wanted_sources([ebay, ali], []) == [ebay]
    assert wanted_sources([ebay, ali], ["aliexpress"]) == [ali]
    assert wanted_sources([ebay, ali], ["ebay", "aliexpress"]) == [ebay, ali]


def test_a_captcha_in_both_pauses_the_source(source):
    source.response = _Resp("<html>punish</html>")
    with pytest.raises(AliExpressBlocked):
        AliExpressSource().search(SearchQuery("gba"))
    source.response = _Resp(PAGE)
    source.browser_page = PAGE
    with pytest.raises(AliExpressBlocked, match="cooldown"):
        AliExpressSource().search(SearchQuery("gba"))
    # The paused search reached neither the site nor the browser.
    assert len(source.calls) == 1 and len(source.browser_calls) == 1


def test_search_counts_the_off_topic_results(source):
    src = AliExpressSource()
    assert src.search(SearchQuery("psone")) == []
    assert src.last_off_topic == 3
