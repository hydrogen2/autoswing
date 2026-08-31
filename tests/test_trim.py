"""Trim framework: rules must not look ahead, and the benchmark must be
buy-and-hold of the same name."""
import pytest
from autoswing.trim import RULES, simulate


def rising(n=200, step=1.01, start=100.0):
    return [start * step**i for i in range(n)]


def falling(n=200, step=0.99, start=100.0):
    return [start * step**i for i in range(n)]


def sawtooth(cycles=20, amp=0.30, start=100.0):
    out = []
    for c in range(cycles):
        for i in range(10):
            out.append(start * (1 + amp * (i / 9)))
        for i in range(10):
            out.append(start * (1 + amp * (1 - i / 9)))
    return out


class TestNoLookAhead:
    def test_rule_cannot_see_the_bar_it_acts_on(self):
        # A rule that saw today's price could dodge a one-day crash exactly.
        # Build a series that is flat then craters on the final bar; every
        # rule must still be holding into it (nothing can dodge it).
        prices = [100.0] * 60 + [40.0]
        for rule in RULES:
            r = simulate("T", prices, rule)
            assert r.ending_shares > 0, f"{rule} dodged an unforeseeable crash"

    def test_identical_prefix_gives_identical_decisions(self):
        a = simulate("T", rising(120), "vol_target_80")
        b = simulate("T", rising(120) + [999.0], "vol_target_80")
        assert b.equity_curve[:119] == a.equity_curve[:119]


class TestBenchmarkSemantics:
    def test_hold_is_exactly_flat_against_itself(self):
        for series in (rising(), falling(), sawtooth()):
            r = simulate("T", series, "hold")
            assert r.vs_hold_pct == 0.0
            assert r.trades == 0

    def test_trimming_a_pure_uptrend_must_lose_to_hold(self):
        # The core honesty check: selling into a riser cannot beat holding it.
        for rule in ("scale_out_ladder", "band_25_15"):
            assert simulate("T", rising(), rule).vs_hold_pct < 0

    def test_trailing_stop_protects_in_a_pure_downtrend(self):
        r = simulate("T", falling(), "trail_25pct")
        assert r.ending_shares == 0
        assert r.vs_hold_pct > 0


class TestAccounting:
    def test_the_two_cost_bases_diverge_and_that_is_the_point(self):
        r = simulate("T", rising(), "scale_out_ladder")
        assert r.ending_shares < r.start_shares
        # Accounting basis barely moves: selling at average cost does not
        # change the average cost of what is left.
        assert r.avg_cost_basis == pytest.approx(100.0, abs=1e-6)
        # The 做波段 basis collapses — proceeds are netted against what is
        # still held. It reaches zero/negative only once proceeds exceed
        # everything paid in; this ladder sells 60% so it lands near $9 on a
        # $100 entry, a ~91% "cost reduction".
        assert r.net_cost_basis < 0.2 * r.avg_cost_basis
        # And yet the position LOST to simply holding. Both facts are true at
        # once; only the second one is about money.
        assert r.vs_hold_pct < 0

    def test_flat_position_reports_no_basis(self):
        r = simulate("T", falling(), "trail_25pct")
        assert r.avg_cost_basis is None and r.net_cost_basis is None

    def test_shares_never_exceed_start_or_go_negative(self):
        for rule in RULES:
            for series in (rising(), falling(), sawtooth()):
                r = simulate("T", series, rule)
                assert 0 <= r.ending_shares <= r.start_shares

    def test_costs_reduce_final_value(self):
        free = simulate("T", sawtooth(), "band_25_15", cost_per_trade=0.0)
        paid = simulate("T", sawtooth(), "band_25_15", cost_per_trade=0.05)
        assert paid.trades == free.trades
        assert paid.final_value < free.final_value
