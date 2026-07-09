"""Adversarial tests: every way a bad proposal might sneak past the gate."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from autoswing.risk_gate import (
    AccountState,
    OpenOrderInfo,
    PositionInfo,
    RiskGate,
    TradeProposal,
)

ET = ZoneInfo("America/New_York")

# Wednesday 2026-07-08, 10:30 ET — regular trading hours.
RTH = datetime(2026, 7, 8, 10, 30, tzinfo=ET)

RISK_CFG = {
    "equity_baseline": 50000,
    "risk_per_trade_pct": 1.0,
    "max_position_pct": 15.0,
    "max_open_positions": 10,
    "max_gross_exposure_pct": 100.0,
    "daily_loss_halt_pct": 3.0,
    "max_drawdown_kill_pct": 15.0,
    "max_core_overlap_positions": 1,
    "min_avg_dollar_volume": 5_000_000,
    "min_price": 5.0,
    "earnings_blackout_days": 5,
    "allow_short": False,
    "allow_outside_rth": False,
    "pdt_min_equity": 25000,
    "core_holdings": ["NVDA", "MSFT"],
}


def make_gate(tmp_path, cfg_overrides=None):
    cfg = {**RISK_CFG, **(cfg_overrides or {})}
    return RiskGate(risk_config=cfg, state_path=tmp_path / "gate_state.json")


def account(net_liq=1_000_000.0, positions=None, open_orders=None):
    return AccountState(
        net_liquidation=net_liq,
        positions=positions or [],
        open_orders=open_orders or [],
    )


def proposal(**overrides):
    """A proposal that passes every rule; tests break one thing at a time.

    Risk: (100-97)*100 = $300 <= $500 budget. Notional $10k... exceeds 15%
    of 50k = $7.5k — so use quantity 60: notional $6k, risk $180.
    """
    base = dict(
        symbol="XOM", action="BUY", quantity=60,
        entry_limit=100.0, stop_loss=97.0, take_profit=112.0,
        rationale="test", next_earnings_date="none",
        avg_dollar_volume=50_000_000.0,
    )
    base.update(overrides)
    return TradeProposal(**base)


def failed_rules(decision):
    return {r.rule for r in decision.rules if not r.passed}


class TestHappyPath:
    def test_clean_proposal_approved(self, tmp_path):
        d = make_gate(tmp_path).evaluate(proposal(), account(), now=RTH)
        assert d.approved, failed_rules(d)
        assert d.virtual_equity == 50000

    def test_virtual_equity_tracks_pnl_not_balance(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)  # anchors
        d = gate.evaluate(proposal(), account(net_liq=1_002_000), now=RTH)
        assert d.virtual_equity == 52000  # +2k P&L on 50k baseline


class TestSizing:
    def test_oversized_risk_rejected(self, tmp_path):
        # 200 shares * $3 stop distance = $600 > $500 budget
        d = make_gate(tmp_path).evaluate(
            proposal(quantity=200), account(), now=RTH
        )
        assert not d.approved
        assert "risk_per_trade" in failed_rules(d)

    def test_oversized_notional_rejected(self, tmp_path):
        # 80 * $100 = $8k > 15% of 50k = $7.5k; stop tightened to keep risk ok
        d = make_gate(tmp_path).evaluate(
            proposal(quantity=80, stop_loss=99.0), account(), now=RTH
        )
        assert not d.approved
        assert "max_position_size" in failed_rules(d)

    def test_absurd_quantity_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(quantity=100000), account(), now=RTH
        )
        assert not d.approved

    def test_penny_stock_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(entry_limit=2.0, stop_loss=1.9, take_profit=2.5, quantity=60),
            account(), now=RTH,
        )
        assert "min_price" in failed_rules(d)


class TestBracketStructure:
    def test_missing_stop_semantics_rejected(self, tmp_path):
        # stop above entry on a BUY = no functioning stop
        d = make_gate(tmp_path).evaluate(
            proposal(stop_loss=105.0), account(), now=RTH
        )
        assert not d.approved
        assert "bracket_structure" in failed_rules(d)

    def test_short_rejected_when_disabled(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(action="SELL", stop_loss=103.0, take_profit=90.0),
            account(), now=RTH,
        )
        assert "short_selling" in failed_rules(d)

    def test_short_allowed_when_enabled(self, tmp_path):
        d = make_gate(tmp_path, {"allow_short": True}).evaluate(
            proposal(action="SELL", stop_loss=103.0, take_profit=90.0),
            account(), now=RTH,
        )
        assert d.approved, failed_rules(d)


class TestPortfolioLimits:
    def test_max_positions_enforced(self, tmp_path):
        positions = [
            PositionInfo(symbol=f"T{i}", quantity=10, notional=1000)
            for i in range(10)
        ]
        d = make_gate(tmp_path).evaluate(
            proposal(), account(positions=positions), now=RTH
        )
        assert "max_open_positions" in failed_rules(d)

    def test_pending_entries_count_toward_position_limit(self, tmp_path):
        held = [PositionInfo(symbol=f"T{i}", quantity=10, notional=1000) for i in range(5)]
        pending = [OpenOrderInfo(symbol=f"P{i}", is_entry=True, notional=1000) for i in range(5)]
        d = make_gate(tmp_path).evaluate(
            proposal(), account(positions=held, open_orders=pending), now=RTH
        )
        assert "max_open_positions" in failed_rules(d)

    def test_gross_exposure_enforced(self, tmp_path):
        positions = [PositionInfo(symbol="T1", quantity=100, notional=46000)]
        d = make_gate(tmp_path).evaluate(
            proposal(), account(positions=positions), now=RTH
        )
        assert "max_gross_exposure" in failed_rules(d)

    def test_duplicate_symbol_rejected(self, tmp_path):
        positions = [PositionInfo(symbol="XOM", quantity=10, notional=1000)]
        d = make_gate(tmp_path).evaluate(
            proposal(), account(positions=positions), now=RTH
        )
        assert "duplicate_position" in failed_rules(d)

    def test_duplicate_pending_order_rejected(self, tmp_path):
        orders = [OpenOrderInfo(symbol="XOM", is_entry=True, notional=1000)]
        d = make_gate(tmp_path).evaluate(
            proposal(), account(open_orders=orders), now=RTH
        )
        assert "duplicate_position" in failed_rules(d)


class TestEarningsBlackout:
    def test_unknown_earnings_date_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(next_earnings_date="unknown"), account(), now=RTH
        )
        assert "earnings_blackout" in failed_rules(d)

    def test_earnings_in_blackout_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(next_earnings_date="2026-07-10"), account(), now=RTH
        )
        assert "earnings_blackout" in failed_rules(d)

    def test_earnings_far_away_ok(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(next_earnings_date="2026-10-20"), account(), now=RTH
        )
        assert d.approved, failed_rules(d)

    def test_garbage_date_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(next_earnings_date="soon"), account(), now=RTH
        )
        assert "earnings_blackout" in failed_rules(d)


class TestLiquidity:
    def test_missing_adv_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(avg_dollar_volume=None), account(), now=RTH
        )
        assert "liquidity" in failed_rules(d)

    def test_illiquid_rejected(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(avg_dollar_volume=500_000), account(), now=RTH
        )
        assert "liquidity" in failed_rules(d)


class TestMarketHours:
    def test_premarket_rejected(self, tmp_path):
        early = datetime(2026, 7, 8, 8, 0, tzinfo=ET)
        d = make_gate(tmp_path).evaluate(proposal(), account(), now=early)
        assert "market_hours" in failed_rules(d)

    def test_weekend_rejected(self, tmp_path):
        saturday = datetime(2026, 7, 11, 11, 0, tzinfo=ET)
        d = make_gate(tmp_path).evaluate(proposal(), account(), now=saturday)
        assert "market_hours" in failed_rules(d)


class TestCoreOverlap:
    def test_first_core_overlap_allowed_with_warning(self, tmp_path):
        d = make_gate(tmp_path).evaluate(
            proposal(symbol="NVDA"), account(), now=RTH
        )
        assert d.approved, failed_rules(d)
        assert any("both books" in w for w in d.warnings)

    def test_second_core_overlap_rejected(self, tmp_path):
        positions = [PositionInfo(symbol="MSFT", quantity=10, notional=3000)]
        d = make_gate(tmp_path).evaluate(
            proposal(symbol="NVDA"), account(positions=positions), now=RTH
        )
        assert "core_overlap" in failed_rules(d)


class TestKillSwitchAndHalts:
    def test_drawdown_trips_kill_switch(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        # -20% of the 50k baseline = net liq down 10k
        d = gate.evaluate(proposal(), account(net_liq=990_000), now=RTH)
        assert not d.approved
        assert "kill_switch" in failed_rules(d)
        assert gate.state.kill_tripped

    def test_kill_switch_persists_across_instances(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        gate.evaluate(proposal(), account(net_liq=990_000), now=RTH)
        # Fresh instance, recovered equity: still tripped until human reset.
        gate2 = make_gate(tmp_path)
        d = gate2.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        assert "kill_switch" in failed_rules(d)

    def test_human_reset_clears_kill(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        gate.evaluate(proposal(), account(net_liq=990_000), now=RTH)
        gate.reset_kill()
        gate2 = make_gate(tmp_path)
        d = gate2.evaluate(proposal(), account(net_liq=990_000), now=RTH)
        assert d.approved, failed_rules(d)

    def test_daily_loss_halts_new_entries(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        # -4% day: 50k -> 48k virtual
        later = datetime(2026, 7, 8, 14, 0, tzinfo=ET)
        d = gate.evaluate(proposal(), account(net_liq=998_000), now=later)
        assert "daily_loss_halt" in failed_rules(d)

    def test_daily_halt_resets_next_day(self, tmp_path):
        gate = make_gate(tmp_path)
        gate.evaluate(proposal(), account(net_liq=1_000_000), now=RTH)
        gate.evaluate(proposal(), account(net_liq=998_000), now=RTH)
        next_day = datetime(2026, 7, 9, 10, 30, tzinfo=ET)
        d = gate.evaluate(proposal(), account(net_liq=998_000), now=next_day)
        assert "daily_loss_halt" not in failed_rules(d)


class TestPDTGuard:
    def test_dormant_above_threshold(self, tmp_path):
        d = make_gate(tmp_path).evaluate(proposal(), account(), now=RTH)
        pdt = next(r for r in d.rules if r.rule == "pdt_guard")
        assert pdt.passed and "dormant" in pdt.detail

    def test_active_below_threshold_blocks_at_three(self, tmp_path):
        gate = make_gate(tmp_path, {"equity_baseline": 20000})
        gate.state.day_trade_dates = ["2026-07-06", "2026-07-07", "2026-07-08"]
        # Keep sizing valid at the smaller baseline.
        d = gate.evaluate(
            proposal(quantity=30), account(net_liq=1_000_000), now=RTH
        )
        assert "pdt_guard" in failed_rules(d)
