"""Tests for deterministic position management."""

from datetime import date

from autoswing.manage import (
    PositionMeta,
    evaluate_position,
    load_meta,
    save_meta,
    trading_days_between,
)

STRAT = {"max_hold_days": 15, "exit_before_earnings_days": 2}
TODAY = date(2026, 7, 9)  # Thursday


class TestTradingDays:
    def test_same_day_zero(self):
        assert trading_days_between(TODAY, TODAY) == 0

    def test_weekend_skipped(self):
        # Fri 2026-07-10 -> Mon 2026-07-13 = 1 trading day
        assert trading_days_between(date(2026, 7, 10), date(2026, 7, 13)) == 1

    def test_full_week(self):
        assert trading_days_between(date(2026, 7, 6), date(2026, 7, 13)) == 5


class TestEvaluatePosition:
    def test_healthy_hold(self):
        action, _ = evaluate_position(
            "XOM", "2026-07-06", "2026-10-30", STRAT, today=TODAY
        )
        assert action == "hold"

    def test_timebox_exit(self):
        action, detail = evaluate_position(
            "XOM", "2026-06-15", "2026-10-30", STRAT, today=TODAY
        )
        assert action == "exit_timebox"

    def test_earnings_buffer_exit(self):
        action, _ = evaluate_position(
            "XOM", "2026-07-06", "2026-07-10", STRAT, today=TODAY
        )
        assert action == "exit_earnings"

    def test_earnings_today_exit(self):
        action, _ = evaluate_position(
            "XOM", "2026-07-06", "2026-07-09", STRAT, today=TODAY
        )
        assert action == "exit_earnings"

    def test_same_day_reaction_entry_holds(self):
        # 2026-07-21 MMM regression: entered on the post-print reaction, so
        # placed_date == next_earnings == today. The print is behind us; a
        # stale feed pinning next_earnings to today must NOT force an exit.
        action, detail = evaluate_position(
            "MMM", "2026-07-09", "2026-07-09", STRAT, today=TODAY
        )
        assert action == "hold", detail

    def test_past_earnings_date_holds(self):
        # Stale feed still shows a now-past print date; it is behind us, so
        # no upcoming gap exposure — hold, don't exit.
        action, detail = evaluate_position(
            "MMM", "2026-07-06", "2026-07-07", STRAT, today=TODAY
        )
        assert action == "hold", detail

    def test_unknown_earnings_forces_exit(self):
        action, detail = evaluate_position(
            "XOM", "2026-07-06", "unknown", STRAT, today=TODAY
        )
        assert action == "exit_earnings"
        assert "unverifiable" in detail

    def test_verified_none_holds(self):
        action, _ = evaluate_position(
            "XOM", "2026-07-06", "none", STRAT, today=TODAY
        )
        assert action == "hold"

    def test_garbage_earnings_date_forces_exit(self):
        action, _ = evaluate_position(
            "XOM", "2026-07-06", "next month", STRAT, today=TODAY
        )
        assert action == "exit_earnings"

    def test_timebox_beats_earnings_check(self):
        # Both apply; time-box fires first, order is deterministic.
        action, _ = evaluate_position(
            "XOM", "2026-06-01", "unknown", STRAT, today=TODAY
        )
        assert action == "exit_timebox"


class TestMetaRoundtrip:
    def test_save_load(self, tmp_path):
        path = tmp_path / "positions.json"
        meta = {
            "XOM": PositionMeta(
                symbol="XOM", placed_date="2026-07-06", entry_limit=100.0,
                stop_loss=97.0, take_profit=112.0, rationale="test",
            )
        }
        save_meta(path, meta)
        loaded = load_meta(path)
        assert loaded["XOM"].placed_date == "2026-07-06"
        assert loaded["XOM"].stop_loss == 97.0

    def test_missing_file_empty(self, tmp_path):
        assert load_meta(tmp_path / "nope.json") == {}
