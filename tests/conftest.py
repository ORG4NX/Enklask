import pytest


@pytest.fixture(autouse=True)
def _no_price_target_check(monkeypatch):
    """The poller and the live feed check price targets after each run through
    their own DB session, which would reach the real database from tests that
    stub only their own session. tests/test_price_alerts.py covers the check.
    """
    monkeypatch.setattr("app.poller.check_price_targets", lambda: 0)
    monkeypatch.setattr("app.livefeed.check_price_targets", lambda: 0)
