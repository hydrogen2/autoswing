"""External-signal ledger: the integrity of this experiment is entirely in
the actionable price and the benchmark. Both are pinned here."""

import json
from datetime import date

import pandas as pd
import pytest

from autoswing.signals import (
    DEFAULT_BENCHMARK,
    aggregate,
    next_tradeable_session,
    score_signal,
    signal_id,
    validate_signal,
)


def frame(closes, start="2026-09-08"):
    return pd.DataFrame({"Close": closes},
                        index=pd.bdate_range(start, periods=len(closes)))


def sig(**kw):
    base = {"id": "src-X-2026-09-04", "source": "src", "symbol": "X",
            "direction": "buy", "signal_date": "2026-09-04",
            "actionable_date": "2026-09-08", "benchmark": "SPY", "note": "n"}
    base.update(kw)
    return base


class TestActionableDate:
    def test_skips_a_market_holiday(self):
        # Friday 09-04 -> Monday 09-07 is Labor Day -> Tuesday 09-08.
        assert next_tradeable_session(date(2026, 9, 4)) == date(2026, 9, 8)

    def test_skips_a_weekend(self):
        assert next_tradeable_session(date(2026, 9, 11)) == date(2026, 9, 14)

    def test_is_strictly_after_the_signal(self):
        # Never same-session: claiming we could act at the post's own price
        # is the easiest way to manufacture an edge that no follower had.
        d = date(2026, 9, 8)
        assert next_tradeable_session(d) > d


class TestValidation:
    def test_clean_signal(self):
        assert validate_signal({"source": "s", "symbol": "X", "direction": "buy",
                                "signal_date": "2026-09-08", "note": "why"}) == []

    def test_direction_and_date_and_note_required(self):
        errs = validate_signal({"source": "s", "symbol": "X",
                                "direction": "hold", "signal_date": "nope",
                                "note": ""})
        assert len(errs) == 3

    def test_id_is_stable_and_namespaced_by_source(self):
        assert signal_id("Serenity", "crdo", "2026-09-08") == "serenity-CRDO-2026-09-08"


class TestScoring:
    def test_alpha_is_net_of_the_benchmark(self):
        stock = frame([100.0] + [110.0] * 10)      # +10% by day 5
        bench = frame([100.0] + [104.0] * 10)      # +4% by day 5
        r = score_signal(sig(), stock, bench)
        assert r["fwd_5d_pct"] == 10.0
        assert r["bench_5d_pct"] == 4.0
        assert r["alpha_5d_pct"] == 6.0

    def test_a_sector_melt_up_is_not_credited_as_skill(self):
        # Same move as the benchmark = zero alpha, not a win.
        s = frame([100.0] + [130.0] * 10)
        b = frame([100.0] + [130.0] * 10)
        assert score_signal(sig(), s, b)["alpha_5d_pct"] == 0.0

    def test_sell_signal_alpha_is_inverted(self):
        # Following a "sell" correctly means the name UNDERperforms.
        s = frame([100.0] + [90.0] * 10)           # -10%
        b = frame([100.0] + [100.0] * 10)          # flat
        r = score_signal(sig(direction="sell"), s, b)
        assert r["fwd_5d_pct"] == -10.0
        assert r["alpha_5d_pct"] == 10.0

    def test_missing_benchmark_keeps_the_return_but_not_a_fake_alpha(self):
        # A benchmark outage must not discard the signal's own record — it
        # would sit "pending" forever with no explanation.
        s = frame([100.0] + [110.0] * 10)
        r = score_signal(sig(), s, None)
        assert r is not None
        assert r["fwd_5d_pct"] == 10.0
        assert r["alpha_5d_pct"] is None
        assert r["benchmark_available"] is False

    def test_unelapsed_horizons_are_none_not_zero(self):
        s = frame([100.0, 101.0, 102.0])           # only 3 bars
        b = frame([100.0, 100.0, 100.0])
        r = score_signal(sig(), s, b)
        assert r is None                            # nothing has elapsed

    def test_scores_from_the_actionable_bar_not_the_signal_bar(self):
        # Bar 0 is the signal day (a big pop we did NOT capture); the
        # actionable bar is the next one. Measuring from bar 0 would credit
        # us with a move that happened before we could act.
        s = frame([200.0, 100.0] + [110.0] * 10, start="2026-09-07")
        b = frame([100.0] * 12, start="2026-09-07")
        r = score_signal(sig(actionable_date="2026-09-08"), s, b)
        assert r["actionable_close"] == 100.0       # not 200.0
        assert r["fwd_5d_pct"] == 10.0


class TestAggregate:
    def rows(self):
        return [
            {"source": "a", "alpha_5d_pct": 2.0, "alpha_15d_pct": 4.0, "alpha_60d_pct": None},
            {"source": "a", "alpha_5d_pct": -1.0, "alpha_15d_pct": None, "alpha_60d_pct": None},
            {"source": "b", "alpha_5d_pct": 5.0, "alpha_15d_pct": None, "alpha_60d_pct": None},
        ]

    def test_grouped_per_source(self):
        out = aggregate(self.rows())
        assert set(out) == {"a", "b"}
        assert out["a"]["n_signals"] == 2

    def test_hit_rate_counts_beating_the_benchmark(self):
        a = aggregate(self.rows())["a"]["h5"]
        assert a["n"] == 2 and a["beat_benchmark"] == 1 and a["hit_rate"] == 0.5
        assert a["mean_alpha_pct"] == 0.5

    def test_unscored_horizon_reports_zero_n_not_a_fake_mean(self):
        assert aggregate(self.rows())["a"]["h60"] == {"n": 0}

    def test_empty(self):
        assert aggregate([]) == {}
