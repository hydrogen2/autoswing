"""Bracket TIF split: entry parent DAY, protective legs GTC.

2026-09-24: CBRL's unfilled entry parent was GTC and silently survived the
night. The obvious fix -- make the bracket DAY -- is the dangerous one: the
same loop set every leg, so a FILLED entry would have lost its stop and
target at the close. These tests pin both halves of the split.
"""

from types import SimpleNamespace

import pytest

from autoswing.broker import Broker, BracketProposal
from autoswing.journal import Journal


class FakeOrder:
    def __init__(self, role):
        self.role = role
        self.tif = "UNSET"
        self.outsideRth = None
        self.orderId = {"entry": 1, "take_profit": 2, "stop_loss": 3}[role]


@pytest.fixture
def placed(tmp_path):
    """Run place_bracket_order against a fake IB and capture the orders."""
    def run(**kw):
        broker = Broker(SimpleNamespace(broker=None), Journal(tmp_path))
        orders = (FakeOrder("entry"), FakeOrder("take_profit"),
                  FakeOrder("stop_loss"))
        sent = []

        def place(contract, order):
            sent.append(order)
            return SimpleNamespace(order=order,
                                   orderStatus=SimpleNamespace(status="Submitted"))

        broker.ib = SimpleNamespace(bracketOrder=lambda **k: orders,
                                    placeOrder=place, sleep=lambda s: None)
        broker._qualified_stock = lambda sym: SimpleNamespace(symbol=sym)
        p = BracketProposal(symbol="TEST", action="BUY", quantity=10,
                            entry_limit=50.0, stop_loss=46.0,
                            take_profit=58.0, **kw)
        broker.place_bracket_order(p)
        return {o.role: o for o in sent}
    return run


def test_entry_parent_defaults_to_day(placed):
    assert placed()["entry"].tif == "DAY"


def test_protective_legs_are_always_gtc(placed):
    """The half that matters most: a filled position must keep its stop and
    target overnight."""
    legs = placed()
    assert legs["stop_loss"].tif == "GTC"
    assert legs["take_profit"].tif == "GTC"


def test_protective_legs_stay_gtc_even_if_entry_is_gtc(placed):
    legs = placed(entry_tif="GTC")
    assert legs["entry"].tif == "GTC"
    assert legs["stop_loss"].tif == "GTC"
    assert legs["take_profit"].tif == "GTC"


def test_all_three_legs_are_sent_and_regular_hours_only(placed):
    legs = placed()
    assert set(legs) == {"entry", "take_profit", "stop_loss"}
    assert all(o.outsideRth is False for o in legs.values())


def test_invalid_entry_tif_rejected_before_anything_is_sent(tmp_path):
    broker = Broker(SimpleNamespace(broker=None), Journal(tmp_path))
    sent = []
    broker.ib = SimpleNamespace(placeOrder=lambda c, o: sent.append(o))
    with pytest.raises(ValueError, match="entry_tif"):
        broker.place_bracket_order(BracketProposal(
            symbol="TEST", action="BUY", quantity=10, entry_limit=50.0,
            stop_loss=46.0, take_profit=58.0, entry_tif="IOC"))
    assert sent == []
