"""Pre-registered news-v2 verdict (owner-approved 2026-09-29).

The rule exists to stop renegotiation, so the tests pin exactly the places a
renegotiation would happen: the sample size, the sample boundary, and what
"survives dropping the best trade" means.
"""

import pytest

from autoswing.shadow import V2_VERDICT_N, v2_verdict


def row(pnl, alpha, closed="2026-10-01"):
    return {"pnl": pnl, "alpha_pct": alpha, "closed": closed}


def test_pending_below_required_n_and_says_so():
    v = v2_verdict([row(-50, -1.0)] * 12)
    assert v["verdict"] == "pending"
    assert v["remaining"] == V2_VERDICT_N - 12
    assert "not a result" in v["note"]


def test_negative_alpha_bins_even_with_positive_pnl():
    rows = [row(100, -0.5)] * V2_VERDICT_N
    v = v2_verdict(rows)
    assert v["verdict"] == "bin"
    assert any("alpha" in r for r in v["reasons"])


def test_negative_pnl_bins_even_with_positive_alpha():
    rows = [row(-10, 0.5)] * V2_VERDICT_N
    v = v2_verdict(rows)
    assert v["verdict"] == "bin"
    assert any("P&L" in r for r in v["reasons"])


def test_robust_positive_result_earns_a_discussion_not_a_promotion():
    rows = [row(40, 0.8)] * V2_VERDICT_N
    v = v2_verdict(rows)
    assert v["verdict"] == "promotion_discussion"


def test_one_outlier_cannot_carry_the_book():
    # 24 small losers and one huge winner: positive overall, nothing without it.
    rows = [row(-20, -0.3)] * (V2_VERDICT_N - 1) + [row(2000, 30.0)]
    v = v2_verdict(rows)
    assert v["total_pnl"] > 0 and v["avg_alpha_pct"] > 0
    assert v["verdict"] == "no_promotion_rests_on_one_trade"


def test_each_criterion_drops_its_own_best_trade():
    """The best-P&L trade and the best-alpha trade differ; each leg must hold
    with its own best removed (the stricter reading)."""
    rows = ([row(10, 0.1)] * (V2_VERDICT_N - 2)
            + [row(1000, 0.0)]      # best by P&L, neutral alpha
            + [row(0, 50.0)])       # best by alpha, flat P&L
    v = v2_verdict(rows)
    # Without its best P&L trade the book still makes 230; without its best
    # alpha trade avg alpha is still positive. Passes.
    assert v["verdict"] == "promotion_discussion"
    rows[-1] = row(0, 200.0)
    rows[:V2_VERDICT_N - 2] = [row(10, -0.5)] * (V2_VERDICT_N - 2)
    v = v2_verdict(rows)
    # Alpha is positive overall only because of the one 200% trade.
    assert v["verdict"] == "no_promotion_rests_on_one_trade"
    assert any("alpha" in r for r in v["reasons"])


def test_verdict_is_frozen_at_the_first_25_closes():
    """The anti-renegotiation test: winners arriving after the 25th close
    must not rescue a binned verdict."""
    first25 = [row(-40, -1.0)] * V2_VERDICT_N
    later = [row(5000, 40.0)] * 10
    v = v2_verdict(first25 + later)
    assert v["verdict"] == "bin"
    assert v["sample_n"] == V2_VERDICT_N
    assert v["closed_total"] == V2_VERDICT_N + 10


def test_missing_alpha_is_never_read_as_passing():
    rows = [{"pnl": 50, "closed": "2026-10-01"}] * V2_VERDICT_N
    v = v2_verdict(rows)
    assert v["verdict"] == "undeterminable"


def test_partial_alpha_is_flagged():
    rows = [row(10, 0.5)] * (V2_VERDICT_N - 1) + [{"pnl": 10, "closed": "x"}]
    v = v2_verdict(rows)
    assert v["alpha_incomplete"] is True


def test_exactly_zero_is_not_below_zero():
    """Literal reading of '< 0', written down so it is not argued later."""
    rows = [row(0, 0.0)] * V2_VERDICT_N
    assert v2_verdict(rows)["verdict"] != "bin"
