"""Research instrument tests: counterfactual exit engine and skip scoring."""

from datetime import date

import pandas as pd

from autoswing.research import (
    LiveTrade,
    compare_exit_rules,
    score_skips,
    simulate_exit,
    validate_skip,
)


def make_df(rows, start="2026-08-10"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in rows], index=idx,
    )


def trade(**kw):
    base = dict(symbol="T", entry_date="2026-08-10", entry=100.0,
                stop=95.0, target=110.0, quantity=48)
    base.update(kw)
    return LiveTrade(**base)


BASELINE = {"name": "b", "target_r": 2.0, "timebox_days": 15}
TRAIL = {"name": "t", "target_r": None, "timebox_days": 15, "trail_r": 1.0}


class TestSimulateExit:
    def test_target_hit_baseline(self):
        # risk = 5, 2R target = 110
        df = make_df([(100, 104, 98, 103), (103, 111, 102, 109)])
        r = simulate_exit(trade(), df, BASELINE)
        assert r["reason"] == "target" and r["exit_price"] == 110.0
        assert r["r_multiple"] == 2.0

    def test_stop_first_on_ambiguous_bar(self):
        df = make_df([(100, 112, 94, 105)])
        r = simulate_exit(trade(), df, BASELINE)
        assert r["reason"] == "stop"

    def test_trailing_stop_locks_in_gains(self):
        # Run to 108 (trail: 108-5=103), then dip to 102 -> stopped at 103.
        df = make_df([(100, 105, 99, 104), (104, 109, 103, 108),
                      (108, 108.5, 101, 102)])
        r = simulate_exit(trade(), df, TRAIL)
        assert r["reason"] == "stop"
        assert r["exit_price"] == 103.0
        assert r["pnl"] > 0  # trailing converted a would-be giveback into profit

    def test_trailing_never_loosens(self):
        # Falling market: trail stays at original stop.
        df = make_df([(100, 101, 96, 97), (97, 98, 94, 95)])
        r = simulate_exit(trade(), df, TRAIL)
        assert r["exit_price"] == 95.0

    def test_still_open_marks_at_last_close(self):
        df = make_df([(100, 104, 98, 103)])
        r = simulate_exit(trade(), df, BASELINE, today=date(2026, 8, 10))
        assert r["reason"] == "still_open"
        assert r["pnl"] == round((103 - 100) * 48, 2)

    def test_timebox_variant_exits_earlier(self):
        rows = [(100, 104, 98, 101)] * 20
        df = make_df(rows)
        r10 = simulate_exit(trade(), df, {"name": "x", "target_r": 2.0,
                                          "timebox_days": 10})
        r15 = simulate_exit(trade(), df, BASELINE)
        assert r10["reason"] == r15["reason"] == "timebox"
        assert r10["exit_date"] < r15["exit_date"]


class TestCompareRules:
    def test_all_rules_reported(self):
        df = make_df([(100, 104, 98, 103), (103, 111, 102, 109)])
        out = compare_exit_rules([trade()], {"T": df}, today=date(2026, 8, 11))
        assert len(out) == 4
        for stats in out.values():
            assert stats["trades"] == 1


class TestSkips:
    def test_validate(self):
        assert validate_skip({"symbol": "X", "category": "sold_off",
                              "reason": "market rejected it"}) == []
        assert validate_skip({"symbol": "X", "category": "vibes",
                              "reason": "r"}) != []

    def test_scoring_and_categories(self):
        rows = [(100, 102, 99, 100)] + [(100, 106, 99, 105)] * 15
        df = make_df(rows)
        skips = [{"symbol": "T", "date": df.index[0].date().isoformat(),
                  "category": "sold_off", "reason": "x"}]
        out = score_skips(skips, {"T": df}, today=date(2026, 9, 15))
        assert out["by_category"]["sold_off"]["n"] == 1
        assert out["scored"][0]["fwd_5d_pct"] == 5.0

    def test_too_recent_stays_pending(self):
        df = make_df([(100, 102, 99, 100)] * 3)
        skips = [{"symbol": "T", "date": df.index[0].date().isoformat(),
                  "category": "capacity", "reason": "x"}]
        out = score_skips(skips, {"T": df}, today=df.index[2].date())
        assert out["pending"] == 1 and out["scored"] == []
