"""Backtest skeleton tests: filters, lookahead-honest entry timing, bracket
simulation, and aggregation — all synthetic, no network."""

import json
from datetime import date

import pandas as pd
import pytest

from autoswing.backtest import (
    DEFAULTS,
    aggregate,
    aggregate_by_year,
    cached_calendar_day,
    calendar_prefilter,
    simulate_candidate,
)
from autoswing.data.earnings import Report

PARAMS = {**DEFAULTS, "min_avg_dollar_volume": 5_000_000.0, "min_price": 5.0}


def report(**overrides):
    base = dict(symbol="TEST", report_date="2024-03-13", timing="unknown",
                eps_actual=1.20, eps_forecast=1.00, surprise_pct=20.0,
                num_estimates=8, market_cap=1e10)
    base.update(overrides)
    return Report(**base)


def make_df(extra_rows, start="2024-02-07"):
    """25 flat pre-report sessions (close 100, vol 1M => $100M ADV), then
    caller-supplied (open, high, low, close, volume) rows. With start
    2024-02-07, session index 25 lands on 2024-03-13 == report_date."""
    rows = [(100, 101, 99, 100, 1_000_000)] * 25 + extra_rows
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
         for o, h, l, c, v in rows], index=idx)


class TestCalendarPrefilter:
    def test_clean_beat_passes(self):
        assert calendar_prefilter([report()], PARAMS) == [report()]

    def test_incomplete_triplet_dropped(self):
        for kw in ({"eps_actual": None}, {"eps_forecast": None},
                   {"surprise_pct": None}):
            assert calendar_prefilter([report(**kw)], PARAMS) == []

    def test_small_surprise_dropped(self):
        assert calendar_prefilter([report(surprise_pct=3.0)], PARAMS) == []

    def test_actual_below_forecast_dropped(self):
        # surprise_pct positive but actual < forecast: self-inconsistent row
        assert calendar_prefilter([report(eps_actual=0.9)], PARAMS) == []

    def test_thin_coverage_dropped(self):
        assert calendar_prefilter([report(num_estimates=2)], PARAMS) == []
        assert calendar_prefilter([report(num_estimates=None)], PARAMS) == []


class TestSimulation:
    def test_target_hit_is_plus_two_r(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),   # D: +8% on 3x volume
            (108, 109, 107.5, 108.5, 1_000_000),  # D+1: quiet drift
            (109, 109.5, 108, 109, 1_000_000),    # D+2: ENTRY at open 109
            (110, 120, 109, 118, 1_500_000),      # target 119 hit
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert t["outcome"] == "trade"
        assert t["entry_date"] == "2024-03-15"  # session after D+1
        assert t["entry_price"] == 109
        assert t["stop"] == 104                  # reaction-day low
        assert t["target"] == pytest.approx(119)
        assert t["exit_reason"] == "target"
        assert t["r_multiple"] == pytest.approx(2.0)

    def test_stop_hit_is_minus_one_r(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 109, 107.5, 108.5, 1_000_000),
            (109, 109.5, 108, 109, 1_000_000),
            (108, 108, 103, 103.5, 1_500_000),   # low 103 < stop 104
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert t["exit_reason"] == "stop"
        assert t["r_multiple"] == pytest.approx(-1.0)

    def test_amc_pattern_reaction_is_next_day(self):
        # flat on D, pop on D+1 -> inferred reaction D+1, entry D+2
        df = make_df([
            (100, 101, 99, 100, 1_000_000),       # D: nothing
            (106, 111, 105, 109, 3_000_000),      # D+1: +9% on 3x volume
            (110, 122, 109, 120, 1_500_000),      # D+2: ENTRY at open 110
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert t["outcome"] == "trade"
        assert t["entry_price"] == 110
        assert t["stop"] == 105                   # D+1's low

    def test_weak_volume_skipped(self):
        df = make_df([
            (105, 110, 104, 108, 1_500_000),      # +8% but only 1.5x volume
            (108, 109, 107.5, 108.5, 1_000_000),
            (109, 109.5, 108, 109, 1_000_000),
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "weak_volume")

    def test_weak_move_skipped(self):
        df = make_df([
            (101, 104, 100, 103, 3_000_000),      # +3% < 5% floor
            (103, 104, 102, 103.5, 1_000_000),
            (104, 104.5, 103, 104, 1_000_000),
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "weak_reaction")

    def test_stop_geometry_skipped(self):
        # reaction low far below entry: stop distance > 8%
        df = make_df([
            (100, 112, 96, 110, 3_000_000),       # +10% but low 96
            (110, 111, 109, 110.5, 1_000_000),
            (111, 112, 110, 111, 1_000_000),      # (111-96)/111 = 13.5%
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "stop_geometry")

    def test_drift_broken_skipped(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),      # D: +8%
            (104, 105, 103.5, 104.2, 1_200_000),  # D+1 close < 108*0.97
            (104, 105, 103, 104, 1_000_000),
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "drift_broken")

    def test_gap_below_stop_skipped(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 109, 107.5, 108.5, 1_000_000),
            (103, 104, 102, 103, 1_000_000),      # opens under stop 104
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "gapped_below_stop")

    def test_open_at_data_end(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 109, 107.5, 108.5, 1_000_000),
            (109, 110, 108, 109.5, 1_000_000),    # entry; nothing hit after
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert t["outcome"] == "open_at_data_end"

    def test_illiquid_skipped(self):
        rows = [(5.5, 5.6, 5.4, 5.5, 100_000)] * 25 + [
            (5.8, 6.2, 5.7, 6.0, 300_000),        # +9% on 3x, but ADV ~$550k
            (6.0, 6.1, 5.9, 6.0, 100_000),
            (6.0, 6.2, 5.9, 6.1, 100_000),
        ]
        idx = pd.bdate_range("2024-02-07", periods=len(rows))
        df = pd.DataFrame([{"Open": o, "High": h, "Low": l, "Close": c,
                            "Volume": v} for o, h, l, c, v in rows], index=idx)
        t = simulate_candidate(report(), df, PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "illiquid")


class TestAggregation:
    def trades(self):
        return [
            {"entry_date": "2024-03-15", "pnl": 100.0, "r_multiple": 2.0,
             "days_held": 3, "exit_reason": "target"},
            {"entry_date": "2024-06-10", "pnl": -50.0, "r_multiple": -1.0,
             "days_held": 2, "exit_reason": "stop"},
            {"entry_date": "2025-01-08", "pnl": 20.0, "r_multiple": 0.4,
             "days_held": 15, "exit_reason": "timebox"},
        ]

    def test_overall(self):
        a = aggregate(self.trades())
        assert a["n"] == 3
        assert a["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert a["avg_r"] == pytest.approx(0.467, abs=1e-3)
        assert a["exit_reasons"] == {"target": 1, "stop": 1, "timebox": 1}

    def test_by_year_partition(self):
        by = aggregate_by_year(self.trades())
        assert set(by) == {"2024", "2025"}
        assert by["2024"]["n"] == 2 and by["2025"]["n"] == 1

    def test_empty(self):
        assert aggregate([]) == {"n": 0}


class TestCalendarCache:
    class FakeSession:
        def __init__(self, rows):
            self.rows, self.calls = rows, 0

        def get(self, *a, **kw):
            self.calls += 1
            rows = self.rows
            return type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"data": {"rows": rows}},
            })()

    def test_fetch_then_cache_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.backtest.CALENDAR_PACING_S", 0)
        rows = [{"symbol": "XOM", "eps": "$1.10", "epsForecast": "$1.00",
                 "surprise": "10", "noOfEsts": "8", "time": "time-not-supplied",
                 "name": "Exxon", "marketCap": "$400,000,000,000"}]
        s = self.FakeSession(rows)
        d = date(2024, 3, 13)
        first = cached_calendar_day(d, tmp_path, s)
        second = cached_calendar_day(d, tmp_path, s)
        assert s.calls == 1  # second read came from disk
        assert first[0].symbol == second[0].symbol == "XOM"
        assert first[0].eps_forecast == 1.00

    def test_cache_stores_raw_rows_not_parsed(self, tmp_path, monkeypatch):
        # parser fixes must apply to already-cached days on reread
        monkeypatch.setattr("autoswing.backtest.CALENDAR_PACING_S", 0)
        s = self.FakeSession([{"symbol": "A", "eps": "-$1.00"}])
        cached_calendar_day(date(2024, 3, 13), tmp_path, s)
        raw = json.loads((tmp_path / "2024-03-13.json").read_text())
        assert raw[0]["eps"] == "-$1.00"


class TestEntryModes:
    """Completed-session filter (pre-registered 2026-10-05)."""

    CONFIRM = {**PARAMS, "entry_mode": "confirm"}
    DELAY = {**PARAMS, "entry_mode": "delay"}

    def test_confirm_enters_two_sessions_after_the_reaction_when_it_holds(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),     # D = reaction, close 108
            (108, 109, 107.5, 108.5, 1_000_000), # D+1 closes 108.5 >= 108
            (109, 109.5, 108, 109, 1_000_000),   # D+2: ENTRY at open 109
            (110, 120, 109, 118, 1_500_000),
        ])
        t = simulate_candidate(report(), df, self.CONFIRM)
        assert t["outcome"] == "trade" and t["entry_date"] == "2024-03-15"

    def test_confirm_skips_a_small_pullback_the_skeleton_tolerates(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 108.5, 106.5, 107, 1_000_000), # D+1 closes 107 < 108 (-0.9%)
            (107, 108, 106.5, 107.5, 1_000_000),
            (108, 119, 107, 118, 1_500_000),
        ])
        assert simulate_candidate(report(), df, PARAMS)["outcome"] == "trade"
        t = simulate_candidate(report(), df, self.CONFIRM)
        assert (t["outcome"], t["reason"]) == ("skip", "no_confirmation")

    def test_equal_close_confirms(self):
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 109, 107.5, 108, 1_000_000),   # exactly the reaction close
            (109, 109.5, 108, 109, 1_000_000),
            (110, 120, 109, 118, 1_500_000),
        ])
        assert simulate_candidate(report(), df, self.CONFIRM)["outcome"] == "trade"

    def test_after_close_pattern_waits_one_more_session_than_the_skeleton(self):
        """Reaction on D+1: the skeleton enters D+2 with NO completed session
        behind it. Confirm needs D+2 to close holding, then enters D+3."""
        df = make_df([
            (100, 101, 99, 100, 1_000_000),      # D: nothing
            (106, 111, 105, 109, 3_000_000),     # D+1 = reaction, close 109
            (110, 111, 108.5, 110, 1_000_000),   # D+2 closes 110 >= 109
            (110.5, 112, 110, 111, 1_000_000),   # D+3: confirm ENTRY at 110.5
            (111, 124, 110, 122, 1_500_000),
        ])
        skel = simulate_candidate(report(), df, PARAMS)
        conf = simulate_candidate(report(), df, self.CONFIRM)
        assert skel["entry_date"] == "2024-03-15" and skel["entry_price"] == 110
        assert conf["entry_date"] == "2024-03-18" and conf["entry_price"] == 110.5

    def test_delay_waits_but_keeps_the_looser_pullback_tolerance(self):
        df = make_df([
            (100, 101, 99, 100, 1_000_000),
            (106, 111, 105, 109, 3_000_000),     # reaction close 109
            (109, 109.5, 107, 108, 1_000_000),   # -0.9%: delay ok, confirm not
            (108.5, 110, 108, 109.5, 1_000_000),
            (110, 124, 109, 122, 1_500_000),
        ])
        assert simulate_candidate(report(), df, self.DELAY)["entry_date"] == "2024-03-18"
        assert simulate_candidate(report(), df, self.CONFIRM)["reason"] == "no_confirmation"

    def test_open_entry_counts_a_first_day_stop_out(self):
        """The backtest enters AT the open, so an intraday stop on the entry
        day is real even if the close recovers."""
        df = make_df([
            (105, 110, 104, 108, 3_000_000),
            (108, 109, 107.5, 108.5, 1_000_000),
            (109, 109.5, 103, 108, 1_000_000),   # entry day: low 103 < stop 104
            (110, 120, 109, 118, 1_500_000),
        ])
        t = simulate_candidate(report(), df, PARAMS)
        assert t["exit_reason"] == "stop" and t["exit_date"] == "2024-03-15"


class TestConfirmVerdict:
    @staticmethod
    def res(n, avg, tot, years):
        return {"overall": {"n": n, "hit_rate": 0.4, "avg_r": avg, "total_r": tot},
                "by_year": {y: {"total_r": v} for y, v in years.items()}}

    def test_supports_only_when_avg_total_and_two_years_all_improve(self):
        from autoswing.backtest import confirm_verdict
        s = self.res(100, 0.05, 5.0, {"2023": -5, "2024": 6, "2025": 4})
        c = self.res(60, 0.15, 9.0, {"2023": -1, "2024": 7, "2025": 3})
        assert confirm_verdict(s, c)["verdict"] == "supports_hard_rule"

    def test_better_selection_that_makes_less_is_not_a_hard_rule_case(self):
        from autoswing.backtest import confirm_verdict
        s = self.res(100, 0.05, 5.0, {"2023": -5, "2024": 6, "2025": 4})
        c = self.res(30, 0.10, 3.0, {"2023": -1, "2024": 2, "2025": 2})
        assert confirm_verdict(s, c)["verdict"] == "better_per_trade_only"

    def test_one_good_year_cannot_carry_it(self):
        from autoswing.backtest import confirm_verdict
        s = self.res(100, 0.05, 5.0, {"2023": -5, "2024": 6, "2025": 4})
        c = self.res(60, 0.15, 9.0, {"2023": 8, "2024": 0, "2025": 1})
        assert confirm_verdict(s, c)["verdict"] == "better_per_trade_only"

    def test_lower_avg_r_is_no_support(self):
        from autoswing.backtest import confirm_verdict
        s = self.res(100, 0.05, 5.0, {"2023": 1, "2024": 2, "2025": 2})
        c = self.res(60, 0.04, 6.0, {"2023": 2, "2024": 2, "2025": 2})
        assert confirm_verdict(s, c)["verdict"] == "no_support"


class TestDelayVerdict:
    """Pre-registered 2026-10-05 for the unseen 2021-22 run."""

    @staticmethod
    def res(avg, halves):
        trades = [{"entry_date": f"{h[:4]}-{'03' if h.endswith('H1') else '09'}-15",
                   "r_multiple": v} for h, v in halves.items()]
        tot = round(sum(halves.values()), 2)
        return {"overall": {"n": 100, "hit_rate": 0.4, "avg_r": avg, "total_r": tot},
                "trades": trades}

    H = ("2021H1", "2021H2", "2022H1", "2022H2")

    def test_half_year_bucketing(self):
        from autoswing.backtest import half_year_totals
        t = [{"entry_date": "2021-06-30", "r_multiple": 1.0},
             {"entry_date": "2021-07-01", "r_multiple": 2.0},
             {"entry_date": "2022-01-03", "r_multiple": -1.0}]
        assert half_year_totals(t) == {"2021H1": 1.0, "2021H2": 2.0, "2022H1": -1.0}

    def test_supports_needs_avg_total_and_three_of_four_halves(self):
        from autoswing.backtest import delay_verdict
        s = self.res(0.05, dict(zip(self.H, (5, -10, 3, 2))))
        d = self.res(0.12, dict(zip(self.H, (6, -2, 4, 1))))
        v = delay_verdict(s, d)
        assert v["verdict"] == "supports" and len(v["half_years_total_r_higher"]) == 3

    def test_two_of_four_halves_is_not_enough(self):
        from autoswing.backtest import delay_verdict
        s = self.res(0.05, dict(zip(self.H, (5, -10, 3, 2))))
        d = self.res(0.12, dict(zip(self.H, (4, 9, 2, 3))))
        assert delay_verdict(s, d)["verdict"] == "better_per_trade_only"

    def test_lower_total_is_not_support_even_with_better_average(self):
        from autoswing.backtest import delay_verdict
        s = self.res(0.05, dict(zip(self.H, (5, 5, 5, 5))))
        d = self.res(0.12, dict(zip(self.H, (6, 6, 6, -9))))
        assert delay_verdict(s, d)["verdict"] == "better_per_trade_only"

    def test_lower_average_is_no_support(self):
        from autoswing.backtest import delay_verdict
        s = self.res(0.05, dict(zip(self.H, (1, 1, 1, 1))))
        d = self.res(0.04, dict(zip(self.H, (2, 2, 2, 2))))
        assert delay_verdict(s, d)["verdict"] == "no_support"
