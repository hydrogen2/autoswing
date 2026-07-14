"""Regression: reconciliation must not trust a blank position feed.

Overnight (2026-07-14 ~03:45 UTC, the gateway's nightly restart window)
IB reported positions=[] for an account that demonstrably still held PENG:
cash was unchanged and both bracket exit legs were still working. Running
manage-positions in that state used to delete the position's metadata —
stops, time-box start, rationale — and journal it as closed.

A real close can never look like that: a bracket fill cancels the other
leg, so "position gone but exit orders live" means the snapshot is broken.
"""

import json

import pytest

from autoswing.cli import _manage_positions
from autoswing.journal import Journal
from autoswing.manage import PositionMeta, load_meta, save_meta

STRAT = {"max_hold_days": 15, "exit_before_earnings_days": 2}

PENG_ORDERS = [
    {"order_id": 15, "symbol": "PENG", "action": "SELL", "type": "LMT",
     "quantity": 96.0, "limit_price": 86.6, "stop_price": 0.0,
     "status": "PreSubmitted"},
    {"order_id": 16, "symbol": "PENG", "action": "SELL", "type": "STP",
     "quantity": 96.0, "limit_price": 0.0, "stop_price": 71.0,
     "status": "PreSubmitted"},
]


class StubConfig:
    strategy = STRAT


class StubBroker:
    def __init__(self, journal, positions, open_orders):
        self.journal = journal
        self.config = StubConfig()
        self._snapshot = {"positions": positions, "open_orders": open_orders}
        self.closed = []

    def get_positions(self):
        return self._snapshot

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"closed": symbol}


@pytest.fixture
def meta_path(tmp_path):
    path = tmp_path / "positions.json"
    save_meta(path, {
        "PENG": PositionMeta(
            symbol="PENG", placed_date="2026-07-10", entry_limit=76.2,
            stop_loss=71.0, take_profit=86.6, rationale="test",
        )
    })
    return path


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal")


def _events(journal):
    files = list(journal.dir.glob("*.jsonl"))
    lines = [l for f in files for l in f.read_text().splitlines()]
    return [json.loads(l)["event"] for l in lines]


class TestBlankSnapshotGuard:
    def test_blackout_keeps_meta(self, journal, meta_path):
        broker = StubBroker(journal, positions=[], open_orders=PENG_ORDERS)
        result = _manage_positions(broker, enforce=False, meta_path=meta_path)

        kept = load_meta(meta_path)
        assert "PENG" in kept
        assert kept["PENG"].stop_loss == 71.0
        assert "manage.position_closed" not in _events(journal)
        assert "manage.snapshot_suspect" in _events(journal)
        assert result["positions"][0]["action"] == "hold"

    def test_blackout_never_enforces_close(self, journal, meta_path):
        broker = StubBroker(journal, positions=[], open_orders=PENG_ORDERS)
        _manage_positions(broker, enforce=True, meta_path=meta_path)
        assert broker.closed == []
        assert "PENG" in load_meta(meta_path)

    def test_real_close_still_reconciled(self, journal, meta_path):
        # Position gone AND no working orders: a genuine close (bracket
        # fill cancels the sibling leg) — metadata must still be dropped.
        broker = StubBroker(journal, positions=[], open_orders=[])
        _manage_positions(broker, enforce=False, meta_path=meta_path)
        assert load_meta(meta_path) == {}
        assert "manage.position_closed" in _events(journal)

    def test_held_position_unaffected(self, journal, meta_path, monkeypatch):
        import autoswing.data.earnings as earnings
        monkeypatch.setattr(earnings, "next_earnings_date",
                            lambda sym: "2026-10-13")
        broker = StubBroker(
            journal,
            positions=[{"symbol": "PENG", "quantity": 96.0, "avg_cost": 76.21}],
            open_orders=PENG_ORDERS,
        )
        result = _manage_positions(broker, enforce=False, meta_path=meta_path)
        assert "PENG" in load_meta(meta_path)
        assert "manage.snapshot_suspect" not in _events(journal)
        assert result["positions"][0]["symbol"] == "PENG"
