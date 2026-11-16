import pytest

from app.sources.vinted_api import VintedApiClient, VintedApiError, catalog_api_host


class _Cookie:
    def __init__(self, name, value):
        self.name, self.value = name, value


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    """Scripted curl_cffi stand-in: answers per URL prefix, records calls."""

    def __init__(self, answers, cookies=(("anon_id", "anon-1"), ("access_token_web", "tok"))):
        self.answers = answers
        self.calls = []
        self.cookies = [_Cookie(n, v) for n, v in cookies]

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for prefix, queue in self.answers.items():
            if url.startswith(prefix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected request {url}")


def _client(*sessions):
    made = list(sessions)
    return VintedApiClient("https://www.vinted.fr", session_factory=lambda: made.pop(0))


def test_catalog_api_host():
    assert catalog_api_host("www.vinted.fr") == "api.vinted.fr"
    assert catalog_api_host("www.vinted.com") == "api.www.vinted.com"


def test_search_warms_up_then_queries_the_catalogue_api_as_the_site_does():
    session = _Session({
        "https://www.vinted.fr/help": [_Resp(200)],
        "https://api.vinted.fr/svc-catalogue/items": [_Resp(200, {"items": [{"id": 1}, "junk"]})],
    })
    items = _client(session).search({"search_text": "3ds", "order": "newest_first"})

    assert items == [{"id": 1}]
    api = session.calls[1]
    assert api["params"]["search_text"] == "3ds"
    assert api["params"]["per_page"] == 20 and api["params"]["currency"] == "EUR"
    # Host-only www cookies are handed to the API host explicitly.
    assert "anon_id=anon-1" in api["headers"]["Cookie"]
    assert api["headers"]["X-Anon-Id"] == "anon-1"
    assert api["headers"]["Locale"] == "fr-FR"


def test_session_is_reused_between_searches():
    session = _Session({
        "https://www.vinted.fr/help": [_Resp(200)],
        "https://api.vinted.fr/": [_Resp(200, {"items": []})],
    })
    client = _client(session)
    client.search({"search_text": "a"})
    client.search({"search_text": "b"})
    assert [c["url"].split("?")[0] for c in session.calls].count("https://www.vinted.fr/help") == 1


def test_refused_session_is_renewed_once():
    first = _Session({
        "https://www.vinted.fr/help": [_Resp(200)],
        "https://api.vinted.fr/": [_Resp(403)],
    })
    second = _Session({
        "https://www.vinted.fr/help": [_Resp(200)],
        "https://api.vinted.fr/": [_Resp(200, {"items": [{"id": 7}]})],
    })
    assert _client(first, second).search({"search_text": "x"}) == [{"id": 7}]


def test_missing_help_page_falls_back_to_home():
    session = _Session({
        "https://www.vinted.fr/help": [_Resp(404)],
        "https://www.vinted.fr/": [_Resp(200)],
        "https://api.vinted.fr/": [_Resp(200, {"items": []})],
    })
    _client(session).search({"search_text": "x"})
    assert session.calls[1]["url"] == "https://www.vinted.fr/"


def test_errors_raise_with_the_status():
    session = _Session({
        "https://www.vinted.fr/help": [_Resp(200)],
        "https://api.vinted.fr/": [_Resp(429)],
    })
    with pytest.raises(VintedApiError) as exc:
        _client(session).search({"search_text": "x"})
    assert exc.value.status == 429

    blocked = _Session({"https://www.vinted.fr/help": [_Resp(403)]})
    with pytest.raises(VintedApiError):
        _client(blocked).search({"search_text": "x"})
