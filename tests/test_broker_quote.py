"""get_quote under market-data denial (IB 10197 / 354).

2026-09-09/10: a competing live session refused every quote request with
error 10197. get_quote sat out its full 15s poll and journaled an all-null
quote with no failure reason — a refusal rendered as a quiet market. The
denial must cut the poll short and label the result.
"""

from types import SimpleNamespace

import pytest

from autoswing.broker import Broker
from autoswing.journal import Journal

NAN = float("nan")


class FakeTicker:
    def __init__(self):
        self.bid = NAN
        self.ask = NAN
        self.last = NAN
        self.close = NAN
        self.volume = NAN
        self.marketDataType = 1  # ib_async default: never updated on denial


@pytest.fixture
def broker(tmp_path):
    config = SimpleNamespace(broker=None)  # connect() is never called
    return Broker(config, Journal(tmp_path))


def _wire_fake_ib(broker, ticker, on_sleep):
    contract = SimpleNamespace(symbol="AVAV")
    broker.ib = SimpleNamespace(
        reqMktData=lambda *a, **k: ticker,
        cancelMktData=lambda c: None,
        sleep=on_sleep,
    )
    broker._qualified_stock = lambda symbol: contract
    return contract


def test_denial_aborts_poll_and_labels_quote(broker):
    ticker = FakeTicker()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        broker._on_api_message(
            292, 10197, "No market data during competing live session",
            SimpleNamespace(symbol="AVAV"),
        )

    _wire_fake_ib(broker, ticker, sleep)
    quote = broker.get_quote("avav")

    assert len(sleeps) == 1  # aborted on first denial, not 30 polls
    assert quote["error"] == "10197: No market data during competing live session"
    assert quote["bid"] is None and quote["last"] is None and quote["close"] is None
    # The denial is consumed: it must not leak into a later, healthy quote.
    assert broker._data_denied == {}


def test_data_beats_stale_denial(broker):
    ticker = FakeTicker()

    def sleep(seconds):
        ticker.last = 152.0
        ticker.close = 151.3

    contract = _wire_fake_ib(broker, ticker, sleep)
    # A denial left over from an earlier request must not taint this one.
    broker._data_denied[contract.symbol] = "10197: stale"
    quote = broker.get_quote("AVAV")

    assert "error" not in quote
    assert quote["last"] == 152.0


def test_benign_notice_does_not_abort_poll(broker):
    ticker = FakeTicker()
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        # 10167 "displaying delayed data" is a notice, not a refusal
        # (the 2026-08-12 lesson: notices arrive on the error channel).
        broker._on_api_message(
            292, 10167, "Requested market data is not subscribed. Displaying delayed market data.",
            SimpleNamespace(symbol="AVAV"),
        )
        if len(polls) == 3:
            ticker.last = 152.0

    _wire_fake_ib(broker, ticker, sleep)
    quote = broker.get_quote("AVAV")

    assert quote["last"] == 152.0
    assert "error" not in quote
