"""Pre-registered post-#25 out-of-sample v2 verdict (registered 2026-11-06).

Same anti-renegotiation intent as the first-25 tests: pin the sample
boundary on BOTH sides (the first 25 never count; closes after the
period's 25th never count), and pin that the two registrations are
independent — neither verdict can move the other.
"""

from autoswing.shadow import (
    V2_OOS_VERDICT_N, V2_VERDICT_N, v2_oos_verdict, v2_verdict,
)


def row(pnl, alpha, closed="2026-11-10"):
    return {"pnl": pnl, "alpha_pct": alpha, "closed": closed}


FIRST25 = [row(-40, -1.0, closed="2026-10-01")] * V2_VERDICT_N


def test_period_opens_empty_after_the_first_25():
    v = v2_oos_verdict(FIRST25)
    assert v["verdict"] == "pending"
    assert v["sample_n"] == 0
    assert v["period_closed"] == 0
    assert v["remaining"] == V2_OOS_VERDICT_N


def test_the_first_25_never_count_toward_the_oos_sample():
    # A catastrophic first 25 with a clean positive OOS period: the OOS
    # verdict must see only the period's rows.
    oos = [row(40, 0.8)] * V2_OOS_VERDICT_N
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["sample_n"] == V2_OOS_VERDICT_N
    assert v["total_pnl"] == 40 * V2_OOS_VERDICT_N
    assert v["verdict"] == "promotion_discussion"


def test_closes_after_the_periods_25th_never_count():
    """The anti-renegotiation test, mirrored from the first sample: winners
    arriving after the OOS period's 25th close must not rescue a bin."""
    oos = [row(-40, -1.0)] * V2_OOS_VERDICT_N
    later = [row(5000, 40.0)] * 10
    v = v2_oos_verdict(FIRST25 + oos + later)
    assert v["verdict"] == "bin"
    assert v["sample_n"] == V2_OOS_VERDICT_N
    assert v["period_closed"] == V2_OOS_VERDICT_N + 10


def test_registrations_are_independent_in_both_directions():
    """Neither verdict reads the other's rows: first-25 stays binned however
    the OOS period goes, and the OOS verdict is identical whatever the first
    25 looked like."""
    oos = [row(40, 0.8)] * V2_OOS_VERDICT_N
    assert v2_verdict(FIRST25 + oos)["verdict"] == "bin"
    great_first25 = [row(40, 0.8, closed="2026-10-01")] * V2_VERDICT_N
    assert v2_oos_verdict(FIRST25 + oos) == v2_oos_verdict(great_first25 + oos)


def test_pending_shows_progress_but_is_not_a_result():
    # The live state at registration: CRL and HHH, 2/25.
    oos = [row(-330.54, -3.45, closed="2026-11-05"),
           row(219.09, 6.45, closed="2026-11-03")]
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["verdict"] == "pending"
    assert v["sample_n"] == 2
    assert v["remaining"] == V2_OOS_VERDICT_N - 2
    assert v["total_pnl"] == -111.45
    assert "not a result" in v["note"]


def test_same_criteria_negative_alpha_bins_despite_positive_pnl():
    oos = [row(100, -0.5)] * V2_OOS_VERDICT_N
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["verdict"] == "bin"
    assert any("alpha" in r for r in v["reasons"])


def test_same_criteria_one_outlier_cannot_carry_the_period():
    oos = [row(-20, -0.3)] * (V2_OOS_VERDICT_N - 1) + [row(2000, 30.0)]
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["total_pnl"] > 0 and v["avg_alpha_pct"] > 0
    assert v["verdict"] == "no_promotion_rests_on_one_trade"


def test_missing_alpha_is_never_read_as_passing():
    oos = [{"pnl": 50, "closed": "2026-11-10"}] * V2_OOS_VERDICT_N
    assert v2_oos_verdict(FIRST25 + oos)["verdict"] == "undeterminable"
