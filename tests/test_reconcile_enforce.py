"""The reconciler's EXECUTION branch — the half 439 shadow runs never touched.

Shadow mode proved the DECISION logic does not false-positive (zero decisions
in 18 days post-f2721d3, while correctly declining 9 pending entries and 2
transient suspects). It could not prove that enforcing a decision does the
right thing, because enforcing never ran. These tests cover that gap before
promotion, since the path cancels live protective orders and the 2026-07-14
naked short came from orphaned stops.
"""

import json
from types import SimpleNamespace

import pytest

from autoswing.commands.trading import _reconcile
from autoswing.journal import Journal


class FakeBroker:
    """Records broker mutations instead of performing them."""

    def __init__(self, tmp_path, mode, positions, orders, cash=50000.0, fills=None):
        self.config = SimpleNamespace(reconcile={"mode": mode, "min_polls": 2,
                                                 "min_suspect_minutes": 90})
        self.journal = Journal(tmp_path / "journal")
        self._positions = positions
        self._orders = orders
        self._cash = cash
        self._fills = fills or []
        self.cancelled = []
        self.stops_placed = []

    def get_positions(self):
        return {"positions": self._positions, "open_orders": self._orders}

    def get_account(self):
        return {"summary": {"TotalCashValue": {"value": self._cash}}}

    def recent_fills(self):
        return self._fills

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"order_id": oid, "status": "Cancelled"}

    def place_protective_stop(self, symbol, quantity, stop_price):
        self.stops_placed.append((symbol, quantity, stop_price))
        return {"symbol": symbol, "quantity": quantity, "stop_price": stop_price}


def order(oid, sym, action, otype, qty=96):
    return {"order_id": oid, "symbol": sym, "action": action,
            "type": otype, "quantity": qty, "status": "PreSubmitted"}


def aged_state(tmp_path, symbol, status, minutes_ago=120, notional=6816.0):
    """Persist a suspicion old enough that the next poll confirms it."""
    from datetime import datetime, timedelta, timezone
    seen = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    p = tmp_path / "state"
    p.mkdir(parents=True, exist_ok=True)
    (p / "reconcile_state.json").write_text(json.dumps({
        symbol: {"status": status, "first_seen": seen, "polls": 3,
                 "cash_at_first": 50000.0, "est_notional": notional}}))


def write_meta(tmp_path, symbol, stop_loss):
    p = tmp_path / "state"
    p.mkdir(parents=True, exist_ok=True)
    (p / "positions.json").write_text(json.dumps({symbol: {
        "symbol": symbol, "placed_date": "2026-07-15", "entry_limit": 75.0,
        "stop_loss": stop_loss, "take_profit": 90.0, "rationale": "t",
        "strategy": "pead-v1"}}))


class TestEnforceCancelsOrphans:
    def test_enforce_cancels_exactly_the_orphaned_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        aged_state(tmp_path, "PENG", "suspect_orphan")
        b = FakeBroker(tmp_path, "enforce", positions=[],
                       orders=[order(9, "PENG", "SELL", "STP"),
                               order(8, "PENG", "SELL", "LMT")])
        r = _reconcile(b)
        assert sorted(b.cancelled) == [8, 9]
        assert b.stops_placed == []
        [d] = r["decisions"]
        assert d["action"] == "cancel_orphans" and d["executed"] is True
        assert len(d["results"]) == 2

    def test_shadow_mode_executes_nothing_on_the_same_input(self, tmp_path, monkeypatch):
        # The control: identical state, mode=shadow -> decision recorded,
        # broker untouched. This is what has been running since 07-15.
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        aged_state(tmp_path, "PENG", "suspect_orphan")
        b = FakeBroker(tmp_path, "shadow", positions=[],
                       orders=[order(9, "PENG", "SELL", "STP"),
                               order(8, "PENG", "SELL", "LMT")])
        r = _reconcile(b)
        assert b.cancelled == [] and b.stops_placed == []
        [d] = r["decisions"]
        assert d["executed"] is False and d["note"].startswith("SHADOW")

    def test_a_live_position_is_never_stripped_of_protection(self, tmp_path, monkeypatch):
        # The failure that must never happen: a real position with working
        # legs must produce no cancels, whatever the mode.
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        b = FakeBroker(tmp_path, "enforce",
                       positions=[{"symbol": "PENG", "quantity": 96}],
                       orders=[order(9, "PENG", "SELL", "STP"),
                               order(8, "PENG", "SELL", "LMT")])
        _reconcile(b)
        assert b.cancelled == []

    def test_unfilled_entry_bracket_is_not_cancelled(self, tmp_path, monkeypatch):
        # VOYG 2026-08-06: a working BUY with exit legs and no fill is a
        # PENDING ENTRY, not an orphan. Enforcing on it would cancel live
        # protection — the near-miss that restarted the promotion clock.
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "VOYG", 32.3)
        b = FakeBroker(tmp_path, "enforce", positions=[],
                       orders=[order(1, "VOYG", "BUY", "LMT", 140),
                               order(2, "VOYG", "SELL", "STP", 140),
                               order(3, "VOYG", "SELL", "LMT", 140)])
        _reconcile(b)
        assert b.cancelled == [], "enforce cancelled a pending entry's legs"


class TestEnforceReplacesStops:
    def test_naked_long_gets_its_stop_re_armed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        aged_state(tmp_path, "PENG", "suspect_naked")
        b = FakeBroker(tmp_path, "enforce",
                       positions=[{"symbol": "PENG", "quantity": 96}],
                       orders=[order(8, "PENG", "SELL", "LMT")])   # target only
        r = _reconcile(b)
        assert b.stops_placed == [("PENG", 96, 71.0)]
        assert b.cancelled == []
        [d] = r["decisions"]
        assert d["action"] == "replace_stop" and d["executed"] is True

    def test_stop_quantity_matches_the_position_not_the_intent(self, tmp_path, monkeypatch):
        # A partially-filled or partially-exited position must be re-armed
        # for what is actually held, never for the original size.
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        aged_state(tmp_path, "PENG", "suspect_naked")
        b = FakeBroker(tmp_path, "enforce",
                       positions=[{"symbol": "PENG", "quantity": 40}],
                       orders=[order(8, "PENG", "SELL", "LMT", 40)])
        _reconcile(b)
        assert b.stops_placed == [("PENG", 40, 71.0)]


class TestEnforceBookkeeping:
    def test_mode_is_reported_and_journalled(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        b = FakeBroker(tmp_path, "enforce",
                       positions=[{"symbol": "PENG", "quantity": 96}],
                       orders=[order(9, "PENG", "SELL", "STP"),
                               order(8, "PENG", "SELL", "LMT")])
        r = _reconcile(b)
        assert r["mode"] == "enforce"
        logged = [json.loads(l) for f in (tmp_path / "journal").glob("*.jsonl")
                  for l in f.read_text().splitlines()]
        assert any(e["event"] == "reconcile.report" for e in logged)

    def test_clean_book_produces_no_decisions_and_no_calls(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        write_meta(tmp_path, "PENG", 71.0)
        b = FakeBroker(tmp_path, "enforce",
                       positions=[{"symbol": "PENG", "quantity": 96}],
                       orders=[order(9, "PENG", "SELL", "STP"),
                               order(8, "PENG", "SELL", "LMT")])
        r = _reconcile(b)
        assert r["decisions"] == [] and r["consistent"] is True
        assert b.cancelled == [] and b.stops_placed == []
