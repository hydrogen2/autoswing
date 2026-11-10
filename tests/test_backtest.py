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


# -- short side (registered 2026-11-10) ----------------------------------------

from datetime import timedelta  # noqa: E402

import autoswing.backtest as bt  # noqa: E402
from autoswing.backtest import (  # noqa: E402
    borrow_cost_r, cohort_prices, net_of_borrow, run_backtest, short_verdict,
)

SHORT = {**PARAMS, "side": "short"}


def miss(**overrides):
    return report(**{"eps_actual": 0.80, "eps_forecast": 1.00,
                     "surprise_pct": -20.0, **overrides})


def reflect(rows):
    """Mirror (open, high, low, close, volume) rows around 100: a +8% pop
    becomes a -8% drop, and the bar's high and low trade places."""
    return [(200 - o, 200 - l, 200 - h, 200 - c, v) for o, h, l, c, v in rows]


LONG_WIN = [
    (105, 110, 104, 108, 3_000_000),      # D: +8% on 3x volume
    (108, 109, 107.5, 108.5, 1_000_000),  # D+1
    (109, 109.5, 108, 109, 1_000_000),    # D+2: entry at the open
    (110, 120, 109, 118, 1_500_000),      # target
]
LONG_LOSS = LONG_WIN[:3] + [(108, 108, 103, 103.5, 1_500_000)]


class TestShortPrefilter:
    def test_clean_miss_passes_short_only(self):
        assert calendar_prefilter([miss()], SHORT) == [miss()]
        assert calendar_prefilter([miss()], PARAMS) == []

    def test_beat_dropped_on_short_side(self):
        assert calendar_prefilter([report()], SHORT) == []

    def test_small_miss_dropped(self):
        assert calendar_prefilter([miss(surprise_pct=-3.0)], SHORT) == []

    def test_inconsistent_triplet_dropped(self):
        # surprise says miss, the EPS pair says beat
        assert calendar_prefilter([miss(eps_actual=1.10)], SHORT) == []
        assert calendar_prefilter([miss(eps_actual=1.00)], SHORT) == []

    def test_thin_coverage_dropped(self):
        assert calendar_prefilter([miss(num_estimates=2)], SHORT) == []

    def test_default_side_is_long(self):
        assert DEFAULTS["side"] == "long"


class TestShortSimulation:
    def test_target_hit_is_plus_two_r(self):
        t = simulate_candidate(miss(), make_df(reflect(LONG_WIN)), SHORT)
        assert t["outcome"] == "trade"
        assert t["entry_date"] == "2024-03-15"     # same lookahead rule
        assert t["entry_price"] == 91
        assert t["stop"] == 96                      # reaction-day HIGH
        assert t["target"] == pytest.approx(81)     # 2R BELOW entry
        assert t["exit_reason"] == "target"
        assert t["exit_price"] == pytest.approx(81)
        assert t["r_multiple"] == pytest.approx(2.0)
        assert t["pnl"] == pytest.approx((91 - 81) * int(5000 // 91))

    def test_stop_hit_is_minus_one_r(self):
        t = simulate_candidate(miss(), make_df(reflect(LONG_LOSS)), SHORT)
        assert t["exit_reason"] == "stop"
        assert t["exit_price"] == pytest.approx(96)
        assert t["r_multiple"] == pytest.approx(-1.0)
        assert t["pnl"] < 0

    def test_ambiguous_bar_is_stop_first(self):
        # one bar trades through BOTH the stop (96) and the target (81)
        rows = reflect(LONG_WIN[:3]) + [(91, 97, 80, 85, 2_000_000)]
        t = simulate_candidate(miss(), make_df(rows), SHORT)
        assert t["exit_reason"] == "stop"

    @pytest.mark.parametrize("rows", [LONG_WIN, LONG_LOSS])
    def test_short_is_the_exact_mirror_of_long(self, rows):
        long_t = simulate_candidate(report(), make_df(rows), PARAMS)
        short_t = simulate_candidate(miss(), make_df(reflect(rows)), SHORT)
        for k in ("outcome", "entry_date", "exit_date", "exit_reason",
                  "days_held"):
            assert short_t[k] == long_t[k]
        assert short_t["r_multiple"] == pytest.approx(long_t["r_multiple"])
        assert short_t["entry_price"] == 200 - long_t["entry_price"]
        assert short_t["stop"] == 200 - long_t["stop"]

    def test_up_reaction_is_not_a_short(self):
        t = simulate_candidate(miss(), make_df(LONG_WIN), SHORT)
        assert (t["outcome"], t["reason"]) == ("skip", "weak_reaction")

    def test_down_reaction_is_not_a_long(self):
        t = simulate_candidate(report(), make_df(reflect(LONG_WIN)), PARAMS)
        assert (t["outcome"], t["reason"]) == ("skip", "weak_reaction")

    def test_bounce_before_entry_breaks_the_drift(self):
        rows = reflect(LONG_WIN[:1]) + [
            (93, 96, 92.5, 95.8, 1_200_000),   # close > 92 * 1.03
            (95, 96, 94, 95, 1_000_000),
        ]
        t = simulate_candidate(miss(), make_df(rows), SHORT)
        assert (t["outcome"], t["reason"]) == ("skip", "drift_broken")

    def test_gap_above_stop_skipped(self):
        rows = reflect(LONG_WIN[:2]) + [(97, 98, 96.5, 97, 1_000_000)]
        t = simulate_candidate(miss(), make_df(rows), SHORT)
        assert (t["outcome"], t["reason"]) == ("skip", "gapped_above_stop")

    def test_stop_geometry_skipped(self):
        rows = [
            (100, 104, 88, 90, 3_000_000),     # -10%, but high 104
            (90, 91, 89, 89.5, 1_000_000),
            (89, 90, 88, 89, 1_000_000),       # (104-89)/89 = 16.9%
        ]
        t = simulate_candidate(miss(), make_df(rows), SHORT)
        assert (t["outcome"], t["reason"]) == ("skip", "stop_geometry")

    def test_timebox_covers_at_the_close(self):
        rows = reflect(LONG_WIN[:3]) + [(91, 92, 89, 90, 1_000_000)] * 16
        t = simulate_candidate(miss(), make_df(rows), SHORT)
        assert t["exit_reason"] == "timebox"
        assert t["days_held"] == 15
        assert t["exit_price"] == pytest.approx(90)
        assert t["r_multiple"] == pytest.approx((91 - 90) / (96 - 91))
        assert t["pnl"] > 0

    def test_long_frame_is_not_mutated(self):
        df = make_df(reflect(LONG_WIN))
        before = df.copy()
        simulate_candidate(miss(), df, SHORT)
        pd.testing.assert_frame_equal(df, before)


def strade(entry_date, r, days=1, entry=100.0, stop=105.0):
    ed = date.fromisoformat(entry_date)
    qty = int(5000 // entry)
    return {"entry_date": entry_date,
            "exit_date": (ed + timedelta(days=days)).isoformat(),
            "entry_price": entry, "stop": stop, "r_multiple": r,
            "pnl": round(r * (stop - entry) * qty, 2), "days_held": days,
            "exit_reason": "timebox"}


def sresult(trades, start="2021-01-01", end="2025-12-31", failed=0,
            side="short"):
    return {"params": {"side": side}, "range": {"start": start, "end": end},
            "funnel": {"price_fetch_failed": failed, "skips": {}},
            "trades": trades}


class TestBorrowCost:
    def test_fee_accrues_on_calendar_days(self):
        t = strade("2024-03-15", 0.5, days=21, entry=91.0, stop=96.0)
        # 5%/yr on $91 for 21 days, against $5 of risk per share
        assert borrow_cost_r(t, 5.0) == pytest.approx(
            0.05 * 21 / 365 * 91 / 5)

    def test_same_day_round_trip_is_free(self):
        assert borrow_cost_r(strade("2024-03-15", -1.0, days=0), 5.0) == 0.0

    def test_net_reduces_r_and_dollars_consistently(self):
        t = strade("2024-03-15", 0.5, days=21, entry=91.0, stop=96.0)
        (net,) = net_of_borrow([t], 5.0)
        cost_usd = 0.05 * 21 / 365 * 91 * int(5000 // 91)
        assert net["r_multiple"] == pytest.approx(
            0.5 - borrow_cost_r(t, 5.0), abs=1e-3)
        assert net["pnl"] == pytest.approx(t["pnl"] - cost_usd, abs=0.01)
        assert t["r_multiple"] == 0.5      # the gross trade is untouched

    def test_zero_fee_is_identity(self):
        t = strade("2024-03-15", 0.5, days=21)
        (net,) = net_of_borrow([t], 0.0)
        assert (net["r_multiple"], net["pnl"]) == (t["r_multiple"], t["pnl"])


class TestShortVerdict:
    def test_registration_is_pinned(self):
        # Closed to revision: changing any of these is a dated, deliberate
        # edit that also edits this test.
        assert bt.SHORT_REGISTERED == "2026-11-10"
        assert bt.SHORT_SAMPLE == ("2021-01-01", "2025-12-31")
        assert bt.SHORT_BORROW_ANNUAL_PCT == 5.0
        assert bt.SHORT_BEAR_YEAR == "2022"
        assert bt.SHORT_YEARS_NEEDED == 3

    def test_supports_when_broad_and_not_just_2022(self):
        v = short_verdict(sresult([
            strade("2021-05-03", 1.0), strade("2022-05-03", 1.0),
            strade("2023-05-03", 1.0), strade("2024-05-03", 1.0),
            strade("2025-05-05", -1.0),
        ]))
        assert v["verdict"] == "supports_short_shadow_book"
        assert v["positive_years"] == ["2021", "2022", "2023", "2024"]
        assert v["net_total_r_without_2022"] > 0

    def test_no_support_when_net_average_not_positive(self):
        v = short_verdict(sresult([
            strade("2021-05-03", 1.0), strade("2023-05-03", -1.0)]))
        assert v["verdict"] == "no_support"

    def test_borrow_fee_can_decide_it(self):
        # +0.01R gross, but three weeks of borrow costs more than that
        trades = [strade(f"{y}-05-03", 0.01, days=21)
                  for y in (2021, 2022, 2023, 2024, 2025)]
        v = short_verdict(sresult(trades))
        assert v["gross"]["avg_r"] > 0
        assert v["net"]["avg_r"] < 0
        assert v["verdict"] == "no_support"

    def test_fragile_when_2022_carries_it(self):
        v = short_verdict(sresult([
            strade("2021-05-03", 0.2), strade("2022-05-03", 5.0),
            strade("2023-05-03", 0.2), strade("2024-05-03", 0.2),
            strade("2025-05-05", -1.0),
        ]))
        assert v["net"]["avg_r"] > 0
        assert len(v["positive_years"]) >= 3
        assert v["net_total_r_without_2022"] < 0
        assert v["verdict"] == "positive_but_fragile"

    def test_fragile_when_too_few_positive_years(self):
        v = short_verdict(sresult([
            strade("2021-05-03", 5.0), strade("2022-05-03", -1.0),
            strade("2023-05-03", 3.0), strade("2024-05-03", -1.0),
            strade("2025-05-05", -1.0),
        ]))
        assert v["net_total_r_without_2022"] > 0
        assert v["positive_years"] == ["2021", "2023"]
        assert v["verdict"] == "positive_but_fragile"

    def test_january_2026_entry_counts_overall_not_in_a_year(self):
        v = short_verdict(sresult([
            strade("2021-05-03", 1.0), strade("2023-05-03", 1.0),
            strade("2024-05-03", 1.0), strade("2026-01-02", 2.0),
        ]))
        assert "2026" not in v["net_total_r_by_year"]
        assert v["net"]["n"] == 4
        assert v["net_total_r_without_2022"] == pytest.approx(
            v["net"]["total_r"])

    def test_failed_downloads_block_the_verdict(self):
        good = [strade(f"{y}-05-03", 1.0) for y in (2021, 2022, 2023, 2024)]
        v = short_verdict(sresult(good, failed=7))
        assert v["verdict"] == "undeterminable"
        assert v["price_fetch_failed"] == 7

    def test_no_trades_is_undeterminable(self):
        assert short_verdict(sresult([]))["verdict"] == "undeterminable"

    def test_other_ranges_are_not_judged(self):
        good = [strade(f"{y}-05-03", 1.0) for y in (2021, 2022, 2023, 2024)]
        v = short_verdict(sresult(good, start="2023-01-01"))
        assert v["verdict"] == "unregistered_range"

    def test_long_run_is_never_judged_as_short(self):
        good = [strade(f"{y}-05-03", 1.0) for y in (2021, 2022, 2023, 2024)]
        assert short_verdict(sresult(good, side="long"))["verdict"] == \
            "undeterminable"


class TestPriceOutageGuard:
    DAY = date(2024, 3, 13)

    @staticmethod
    def frame():
        return make_df([])

    def fake(self, monkeypatch, answers):
        """answers: list of dicts returned by successive fetch_window calls."""
        calls = []

        def fetch_window(symbols, start, end):
            calls.append(list(symbols))
            return answers[len(calls) - 1] if len(calls) <= len(answers) else {}

        monkeypatch.setattr(bt, "fetch_window", fetch_window)
        monkeypatch.setattr(bt, "PRICE_PACING_S", 0)
        return calls

    def test_outage_writes_no_markers_and_is_reported(self, tmp_path, monkeypatch):
        calls = self.fake(monkeypatch, [{}, {}])        # cohort, then canary
        failed = []
        out = cohort_prices(self.DAY, ["AAA", "BBB"], tmp_path, failed)
        assert out == {} and failed == ["AAA", "BBB"]
        assert calls == [["AAA", "BBB"], ["SPY"]]
        assert not list(tmp_path.rglob("*.csv"))        # nothing poisoned

    def test_source_up_means_names_are_really_missing(self, tmp_path, monkeypatch):
        self.fake(monkeypatch, [{}, {"SPY": self.frame()}])
        failed = []
        assert cohort_prices(self.DAY, ["AAA", "BBB"], tmp_path, failed) == {}
        assert failed == []
        markers = sorted(p.name for p in tmp_path.rglob("*.csv"))
        assert markers == ["AAA.csv", "BBB.csv"]
        assert all(p.stat().st_size == 0 for p in tmp_path.rglob("*.csv"))

    def test_partial_miss_needs_no_canary(self, tmp_path, monkeypatch):
        calls = self.fake(monkeypatch, [{"AAA": self.frame()}])
        failed = []
        out = cohort_prices(self.DAY, ["AAA", "BBB"], tmp_path, failed)
        assert list(out) == ["AAA"] and failed == [] and len(calls) == 1
        assert (tmp_path / "2024-03-13" / "BBB.csv").stat().st_size == 0

    def test_offline_never_touches_the_network(self, tmp_path, monkeypatch):
        calls = self.fake(monkeypatch, [])
        failed = []
        assert cohort_prices(self.DAY, ["AAA"], tmp_path, failed,
                             offline=True) == {}
        assert calls == [] and failed == ["AAA"]

    def test_run_stops_asking_after_repeated_failures(self, tmp_path, monkeypatch):
        calls = self.fake(monkeypatch, [])              # the source is down
        monkeypatch.setattr(
            bt, "cached_calendar_day",
            lambda day, cache, session: [miss(symbol="AAA",
                                              report_date=day.isoformat())])
        r = run_backtest(date(2024, 3, 11), date(2024, 3, 22),   # 10 weekdays
                         {"min_avg_dollar_volume": 5_000_000.0}, tmp_path,
                         {"side": "short"})
        # 3 failed cohorts (each: cohort + canary), then cache-only
        assert len(calls) == 2 * bt.FETCH_FAILURE_LIMIT
        assert r["funnel"]["price_fetch_failed"] == 10
        assert r["funnel"]["fetch_stopped_after"] == "2024-03-13"
        assert r["funnel"]["skips"] == {"price_fetch_failed": 10}
        assert short_verdict(r)["verdict"] in ("undeterminable",
                                               "unregistered_range")
        assert not list(tmp_path.rglob("*.csv"))

    def test_short_run_carries_the_short_caveats(self, tmp_path, monkeypatch):
        self.fake(monkeypatch, [])
        monkeypatch.setattr(bt, "cached_calendar_day",
                            lambda day, cache, session: [])
        cfg = {"min_avg_dollar_volume": 5_000_000.0}
        short = run_backtest(self.DAY, self.DAY, cfg, tmp_path, {"side": "short"})
        long_ = run_backtest(self.DAY, self.DAY, cfg, tmp_path)
        assert all(c in short["caveats"] for c in bt.SHORT_CAVEATS)
        assert not any(c in long_["caveats"] for c in bt.SHORT_CAVEATS)
        assert long_["funnel"]["price_fetch_failed"] == 0


class TestBacktestShortCommand:
    def test_command_assembles_verdict_and_writes_results(self, tmp_path, monkeypatch):
        import types

        import autoswing.config as cfg
        from autoswing.commands.research import _backtest_short

        seen = []

        def fake_run(start, end, risk, root, overrides=None, progress=None):
            side = (overrides or {}).get("side", "long")
            seen.append(side)
            if progress:
                progress(start, 0)
            trades = [strade(f"{y}-05-03", 1.0, days=10)
                      for y in (2021, 2022, 2023, 2024)]
            return {**sresult(trades, side=side),
                    "overall": aggregate(trades),
                    "by_year": aggregate_by_year(trades), "caveats": []}

        monkeypatch.setattr(bt, "run_backtest", fake_run)
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        records = []
        journal = types.SimpleNamespace(
            record=lambda kind, **kw: records.append((kind, kw)))
        args = types.SimpleNamespace(start="2021-01-01", end="2025-12-31")
        config = types.SimpleNamespace(risk={})
        out = _backtest_short(config, journal, args)

        assert seen == ["short", "long"]          # short first: it is the test
        assert out["verdict"]["verdict"] == "supports_short_shadow_book"
        fees = out["short_by_borrow_fee"]
        assert set(fees) == {"0pct_per_year", "5pct_per_year", "25pct_per_year"}
        assert (fees["0pct_per_year"]["total_r"] > fees["5pct_per_year"]["total_r"]
                > fees["25pct_per_year"]["total_r"])
        assert fees["5pct_per_year"]["total_r"] == out["verdict"]["net"]["total_r"]
        saved = json.loads((tmp_path / "state" / "backtest" /
                            "short-2021-01-01-2025-12-31.json").read_text())
        assert saved["verdict"]["verdict"] == out["verdict"]["verdict"]
        assert records[0][0] == "research.backtest_short"
