"""Suite-wide guards."""

import pytest


@pytest.fixture(autouse=True)
def _no_live_fresh_price(monkeypatch):
    """No test reaches Yahoo for a fresh price by accident.

    get-quote and the wide shadow entry call data.fresh.fresh_price(); with
    the network fetch disabled it answers "unavailable" and callers take
    their labelled fallback, which is also the path most tests predate.
    Tests of the fresh path inject a fetcher or patch fresh_price itself.
    """
    def offline(symbol, timeout):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("autoswing.data.fresh._yahoo_fetch", offline)
