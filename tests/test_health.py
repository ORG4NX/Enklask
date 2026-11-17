import pytest

from app import health


def test_classify_error_tells_blocks_from_other_failures():
    assert health.classify_error("HTTP 403 DataDome device check") == health.BLOCKED
    assert health.classify_error("eBay challenge page (bot check) on www.ebay.fr") == health.BLOCKED
    assert health.classify_error("Vinted catalogue API answered 429") == health.BLOCKED
    assert health.classify_error(TimeoutError("read timed out")) == health.ERROR


def test_snapshot_states():
    assert health.snapshot("vinted") is None

    health.record("vinted", count=20)
    assert health.snapshot("vinted")["state"] == "ok"
    # One empty search among good ones stays green.
    health.record("vinted", count=0)
    assert health.snapshot("vinted")["state"] == "ok"

    health.record("vinted", error=RuntimeError("HTTP 403 DataDome device check"))
    snap = health.snapshot("vinted")
    assert snap["state"] == "blocked" and "DataDome" in snap["last_error"]

    health.record("vinted", error=ValueError("parse failed"))
    assert health.snapshot("vinted")["state"] == "error"


def test_a_few_empty_searches_are_not_enough_to_judge():
    health.record("label_emmaus", count=0)
    health.record("label_emmaus", count=0)
    assert health.snapshot("label_emmaus")["state"] is None


def test_answers_without_results_turn_orange():
    for _ in range(6):
        health.record("easycash", count=0)
    snap = health.snapshot("easycash")
    assert snap["state"] == "empty" and snap["empty_streak"] == 6
    assert snap["last_results_at"] is None


def test_ignored_filters_are_flagged():
    health.record("easycash", count=30, filters_fixed=17)
    snap = health.snapshot("easycash")
    assert snap["state"] == "ok" and snap["filters_ignored"] and snap["filters_fixed"] == 17


def test_state_survives_a_restart(tmp_path, monkeypatch):
    health.record("leboncoin", count=35)
    path = health._path()
    assert path.exists()
    # Simulate a fresh process: forget memory, reload from the file.
    health._state.clear()
    monkeypatch.setattr(health, "_loaded", False)
    assert health.snapshot("leboncoin")["state"] == "ok"


def test_ebay_raises_when_every_domain_is_challenged(monkeypatch):
    from app.sources import ebay
    from app.sources.base import SearchQuery

    source = ebay.EbaySource()
    monkeypatch.setattr(type(source), "_has_api_keys", property(lambda self: False))
    monkeypatch.setattr(
        source, "_fetch_via_curl",
        lambda url, base: "<html><title>Pardon Our Interruption</title></html>",
    )
    with pytest.raises(ebay.EbayBlocked):
        source.search(SearchQuery("switch"))
    # A real page with no matching items is still just "no results".
    monkeypatch.setattr(source, "_fetch_via_curl", lambda url, base: "<html><body></body></html>")
    assert source.search(SearchQuery("switch")) == []
