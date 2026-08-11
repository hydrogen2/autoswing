"""Wide-PEAD shadow ledger: capacity rules must inform, not block; the
measurement series must be standardized and fully separate from the v2 book."""

import json
from argparse import Namespace
from types import SimpleNamespace

import pandas as pd
import pytest

from autoswing.commands.shadow import _shadow_mark, _shadow_paths, _shadow_propose
from autoswing.journal import Journal
from autoswing.risk_gate import AccountState, OpenOrderInfo, PositionInfo
from autoswing.shadow import CAPACITY_RULES, WIDE_NOTIONAL, load_book

RISK_CFG = {
    "equity_baseline": 50000,
    "risk_per_trade_pct": 1.0,
    "max_position_pct": 10.0,
    "max_open_positions": 10,
    "max_gross_exposure_pct": 100.0,
    "daily_loss_halt_pct": 3.0,
    "max_drawdown_kill_pct": 15.0,
    "max_core_overlap_positions": 1,
    "min_avg_dollar_volume": 5_000_000,
    "min_price": 5.0,
    "earnings_blackout_days": 5,
    "allow_short": False,
    "allow_outside_rth": True,  # tests run at arbitrary wall-clock times
    "pdt_min_equity": 25000,
    "core_holdings": ["NVDA", "MSFT"],
}


class FakeBroker:
    """Just enough broker for _shadow_propose: config, journal, account
    snapshot, and a static quote."""

    def __init__(self, tmp_path, account_state):
        self.config = SimpleNamespace(risk=dict(RISK_CFG))
        self.journal = Journal(tmp_path / "journal")
        self._account_state = account_state

    def account_state(self):
        return self._account_state

    def get_quote(self, symbol):
        return {"last": 100.0, "close": 99.5}


def full_account():
    """An account with zero spare capacity: 10 positions, ~98% gross
    exposure, XOM already held. Every capacity rule that can fail, fails."""
    positions = [
        PositionInfo(symbol=s, quantity=10, notional=4900.0)
        for s in ["XOM", "ABT", "TRV", "MMM", "MEDP", "AGYS",
                  "EME", "TILE", "VCTR", "NVDA"]
    ]
    return AccountState(
        net_liquidation=1_000_000.0,
        positions=positions,
        open_orders=[OpenOrderInfo(symbol="CAT", is_entry=True, notional=4900.0)],
    )


def empty_account():
    return AccountState(net_liquidation=1_000_000.0, positions=[], open_orders=[])


def submit(tmp_path, monkeypatch, account, wide, **overrides):
    monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
    payload = dict(
        symbol="XOM", action="BUY", quantity=40,
        entry_limit=100.0, stop_loss=97.0, take_profit=112.0,
        rationale="test", next_earnings_date="none",
        avg_dollar_volume=50_000_000.0,
    )
    payload.update(overrides)
    p = tmp_path / "proposal.json"
    p.write_text(json.dumps(payload))
    broker = FakeBroker(tmp_path, account)
    return _shadow_propose(broker, Namespace(proposal=str(p), wide=wide))


class TestWideCapacityBypass:
    def test_capacity_exhaustion_does_not_block_wide(self, tmp_path, monkeypatch):
        r = submit(tmp_path, monkeypatch, full_account(), wide=True)
        assert r["approved"] and r["opened_virtual"]
        # every capacity failure is preserved as data
        assert set(r["capacity_failures_informational"]) >= {
            "max_open_positions", "max_gross_exposure", "duplicate_position",
        }

    def test_same_account_blocks_non_wide(self, tmp_path, monkeypatch):
        r = submit(tmp_path, monkeypatch, full_account(), wide=False)
        assert not r["approved"] and not r["opened_virtual"]

    def test_strategy_rules_still_block_wide(self, tmp_path, monkeypatch):
        # earnings_blackout "unknown" is a strategy-definition rejection
        r = submit(tmp_path, monkeypatch, full_account(), wide=True,
                   next_earnings_date="unknown")
        assert not r["approved"] and not r["opened_virtual"]

    def test_liquidity_still_blocks_wide(self, tmp_path, monkeypatch):
        r = submit(tmp_path, monkeypatch, full_account(), wide=True,
                   avg_dollar_volume=100_000.0)
        assert not r["approved"]

    def test_capacity_rules_set_is_exactly_the_account_rules(self):
        # market_hours / earnings_blackout / liquidity / bracket_structure /
        # min_price / short_selling / kill_switch must never be waivable.
        assert CAPACITY_RULES == {
            "daily_loss_halt", "risk_per_trade", "max_position_size",
            "max_open_positions", "max_gross_exposure", "duplicate_position",
            "core_overlap", "pdt_guard",
        }


class TestWideStandardization:
    def test_quantity_standardized_to_wide_notional(self, tmp_path, monkeypatch):
        r = submit(tmp_path, monkeypatch, full_account(), wide=True,
                   quantity=999)  # brain-provided size must be ignored
        book = load_book(tmp_path / "state" / "shadow" / "wide_positions.json")
        assert book["XOM"].quantity == int(WIDE_NOTIONAL // 100.0)
        assert r["approved"]

    def test_expensive_stock_gets_at_least_one_share(self, tmp_path, monkeypatch):
        submit(tmp_path, monkeypatch, empty_account(), wide=True,
               entry_limit=6000.0, stop_loss=5820.0, take_profit=6400.0)
        book = load_book(tmp_path / "state" / "shadow" / "wide_positions.json")
        assert book["XOM"].quantity == 1

    def test_default_strategy_tag(self, tmp_path, monkeypatch):
        submit(tmp_path, monkeypatch, empty_account(), wide=True)
        book = load_book(tmp_path / "state" / "shadow" / "wide_positions.json")
        assert book["XOM"].strategy == "pead-wide"


class TestBookSeparation:
    def test_paths_are_distinct(self):
        assert _shadow_paths(wide=True) != _shadow_paths(wide=False)

    def test_wide_never_touches_v2_book(self, tmp_path, monkeypatch):
        submit(tmp_path, monkeypatch, empty_account(), wide=True)
        assert not (tmp_path / "state" / "shadow" / "positions.json").exists()
        assert (tmp_path / "state" / "shadow" / "wide_positions.json").exists()

    def test_v2_never_touches_wide_book(self, tmp_path, monkeypatch):
        r = submit(tmp_path, monkeypatch, empty_account(), wide=False)
        assert r["approved"], r["decision"]
        assert (tmp_path / "state" / "shadow" / "positions.json").exists()
        assert not (tmp_path / "state" / "shadow" / "wide_positions.json").exists()


class TestMarkBothBooks:
    def test_mark_closes_positions_in_both_books(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        # one open position in each book, both destined for a stop
        submit(tmp_path, monkeypatch, empty_account(), wide=False)
        submit(tmp_path, monkeypatch, empty_account(), wide=True,
               symbol="CVX")

        idx = pd.DatetimeIndex([pd.Timestamp.today().normalize()])
        crash = pd.DataFrame(
            [{"Open": 100, "High": 101, "Low": 90, "Close": 91,
              "Volume": 1_000_000}], index=idx)
        monkeypatch.setattr("autoswing.data.prices.fetch_history",
                            lambda syms, period: {s: crash for s in syms})

        config = SimpleNamespace(strategy={"max_hold_days": 15})
        journal = Journal(tmp_path / "journal")
        out = _shadow_mark(config, journal)
        assert [e["symbol"] for e in out["v2"]["closed_today"]] == ["XOM"]
        assert [e["symbol"] for e in out["wide"]["closed_today"]] == ["CVX"]
        assert (tmp_path / "state" / "shadow" / "wide_ledger.jsonl").exists()
        assert (tmp_path / "state" / "shadow" / "ledger.jsonl").exists()

    def test_mark_with_both_books_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.config.PROJECT_ROOT", tmp_path)
        out = _shadow_mark(SimpleNamespace(strategy={}), Journal(tmp_path / "j"))
        assert out == {"v2": {"open": 0, "closed_today": []},
                       "wide": {"open": 0, "closed_today": []}}
