"""Trading calendar + the gate rule that consumes it.

Labor Day 2026-09-07 was the first market holiday since inception and the
gate's market_hours rule passed all session on a closed market. These tests
pin the fix, including the two failure modes a naive holiday list still has:
early closes, and a table that silently expires.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from autoswing.calendar import (
    COVERAGE_THROUGH,
    CalendarExpired,
    is_market_open,
    is_trading_day,
    session_close,
)
from autoswing.risk_gate import AccountState, RiskGate, TradeProposal

ET = ZoneInfo("America/New_York")


class TestTradingDays:
    def test_labor_day_2026_is_closed(self):
        assert not is_trading_day(date(2026, 9, 7))

    def test_ordinary_weekday_is_open(self):
        assert is_trading_day(date(2026, 9, 8))

    def test_weekends_are_closed(self):
        assert not is_trading_day(date(2026, 9, 12))
        assert not is_trading_day(date(2026, 9, 13))

    @pytest.mark.parametrize("d", [
        date(2026, 1, 1), date(2026, 4, 3), date(2026, 6, 19),
        date(2026, 11, 26), date(2026, 12, 25),
        date(2027, 1, 1), date(2027, 5, 31), date(2027, 12, 24),
    ])
    def test_published_closures(self, d):
        assert not is_trading_day(d)

    def test_july_4_observed_not_the_date_itself(self):
        # 2026-07-04 is a Saturday; NYSE observes Friday 07-03.
        assert not is_trading_day(date(2026, 7, 3))


class TestEarlyCloses:
    def test_day_after_thanksgiving_closes_at_one(self):
        assert session_close(date(2026, 11, 27)) == time(13, 0)

    def test_regular_day_closes_at_four(self):
        assert session_close(date(2026, 9, 8)) == time(16, 0)

    def test_open_before_the_early_close(self):
        ok, why = is_market_open(datetime(2026, 11, 27, 12, 0, tzinfo=ET))
        assert ok and "13:00" in why

    def test_closed_after_the_early_close(self):
        # The gap a full-closure list alone would still have let through.
        ok, why = is_market_open(datetime(2026, 11, 27, 14, 0, tzinfo=ET))
        assert not ok and "EARLY CLOSE" in why


class TestExpiryFailsClosed:
    def test_beyond_coverage_raises_rather_than_assuming_open(self):
        beyond = date(COVERAGE_THROUGH.year + 1, 6, 1)
        with pytest.raises(CalendarExpired):
            is_trading_day(beyond)

    def test_the_last_covered_day_still_answers(self):
        assert is_trading_day(COVERAGE_THROUGH) in (True, False)


RISK_CFG = {
    "equity_baseline": 50000, "risk_per_trade_pct": 1.0, "max_position_pct": 10.0,
    "max_open_positions": 10, "max_gross_exposure_pct": 100.0,
    "daily_loss_halt_pct": 3.0, "max_drawdown_kill_pct": 15.0,
    "max_core_overlap_positions": 1, "min_avg_dollar_volume": 5_000_000,
    "min_price": 5.0, "earnings_blackout_days": 5, "allow_short": False,
    "allow_outside_rth": False, "pdt_min_equity": 25000, "core_holdings": [],
}


def gate_verdict(tmp_path, when):
    gate = RiskGate(risk_config=dict(RISK_CFG),
                    state_path=tmp_path / "gate_state.json")
    proposal = TradeProposal(
        symbol="XOM", action="BUY", quantity=60, entry_limit=100.0,
        stop_loss=97.0, take_profit=112.0, rationale="t",
        next_earnings_date="none", avg_dollar_volume=50_000_000.0,
    )
    d = gate.evaluate(proposal, AccountState(1_000_000.0, [], []), now=when)
    return next(r for r in d.rules if r.rule == "market_hours")


class TestGateIntegration:
    def test_labor_day_midsession_is_now_refused(self, tmp_path):
        # The exact live scenario: 09-07 12:00 ET, gate passed it all day.
        r = gate_verdict(tmp_path, datetime(2026, 9, 7, 12, 0, tzinfo=ET))
        assert not r.passed
        assert "holiday" in r.detail

    def test_ordinary_session_still_passes(self, tmp_path):
        r = gate_verdict(tmp_path, datetime(2026, 9, 8, 12, 0, tzinfo=ET))
        assert r.passed

    def test_half_day_afternoon_is_refused(self, tmp_path):
        r = gate_verdict(tmp_path, datetime(2026, 11, 27, 14, 30, tzinfo=ET))
        assert not r.passed

    def test_expired_calendar_refuses_and_says_why(self, tmp_path):
        beyond = datetime(COVERAGE_THROUGH.year + 1, 6, 1, 12, 0, tzinfo=ET)
        r = gate_verdict(tmp_path, beyond)
        assert not r.passed
        assert "calendar covers dates through" in r.detail

    def test_allow_outside_rth_still_overrides(self, tmp_path):
        # Tests run at arbitrary wall-clock times and rely on this escape.
        gate = RiskGate(risk_config={**RISK_CFG, "allow_outside_rth": True},
                        state_path=tmp_path / "g.json")
        proposal = TradeProposal(
            symbol="XOM", action="BUY", quantity=60, entry_limit=100.0,
            stop_loss=97.0, take_profit=112.0, rationale="t",
            next_earnings_date="none", avg_dollar_volume=50_000_000.0)
        d = gate.evaluate(proposal, AccountState(1_000_000.0, [], []),
                          now=datetime(2026, 9, 7, 12, 0, tzinfo=ET))
        assert next(r for r in d.rules if r.rule == "market_hours").passed
