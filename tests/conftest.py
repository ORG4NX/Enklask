import pytest


@pytest.fixture(autouse=True)
def _no_price_target_check(monkeypatch):
    """The poller and the live feed check price targets after each run through
    their own DB session, which would reach the real database from tests that
    stub only their own session. tests/test_price_alerts.py covers the check.
    """
    monkeypatch.setattr("app.poller.check_price_targets", lambda: 0)
    monkeypatch.setattr("app.livefeed.check_price_targets", lambda: 0)
    monkeypatch.setattr("app.poller.check_gone_listings", lambda: 0)


@pytest.fixture(autouse=True)
def _isolated_health(monkeypatch, tmp_path):
    """Source health is mirrored to a JSON file next to the database: keep
    tests on a throwaway file and a clean slate."""
    from app import health

    monkeypatch.setattr(health, "_path", lambda: tmp_path / "source_health.json")
    health.reset()
    yield
    health.reset()
