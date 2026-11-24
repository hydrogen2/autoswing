"""Regime-outcomes read (backlog #6): pin the registered buckets.

Registered 2026-11-20, owner-approved 2026-11-24, before the first read.
Same anti-renegotiation intent as the other registrations: the stressed
boundary (VIX >= 25 OR spy_vs_20dma <= -4%), the untagged-never-guessed
rule, and the per-bucket outputs are fixed; a change here is a revision
and needs the owner's word.
"""

from autoswing.lessons import ClosedTrade
from autoswing.regime import (
    STRESSED_SPY_VS_20DMA_PCT, STRESSED_VIX, classify_mark, regime_outcomes,
)


def mark(vix=15.0, spy=1.0):
    return {"date": "2026-11-20", "vix_close": vix, "spy_vs_20dma_pct": spy}


def trade(sym="AAA", opened="2026-11-10", closed="2026-11-20",
          entry_fill=100.0, stop=95.0, exit_price=110.0):
    return ClosedTrade(
        symbol=sym, placed_date=opened, closed_date=closed,
        entry_limit=100.5, stop_loss=stop, take_profit=115.0,
        rationale="test", strategy="pead-v1",
        exit_price=exit_price, exit_kind="target", entry_fill=entry_fill,
    )


def test_registered_boundary_is_pinned():
    # Revision of either number is an owner decision, not a code change.
    assert STRESSED_VIX == 25.0
    assert STRESSED_SPY_VS_20DMA_PCT == -4.0


def test_classify_stressed_on_either_condition():
    assert classify_mark(mark(vix=25.0, spy=1.0)) == "stressed"   # VIX at bar
    assert classify_mark(mark(vix=35.5, spy=2.0)) == "stressed"
    assert classify_mark(mark(vix=15.0, spy=-4.0)) == "stressed"  # SPY at bar
    assert classify_mark(mark(vix=15.0, spy=-7.4)) == "stressed"


def test_classify_calm_requires_both_inside():
    assert classify_mark(mark(vix=24.99, spy=-3.99)) == "calm"
    assert classify_mark(mark(vix=12.0, spy=3.0)) == "calm"


def test_classify_untagged_never_guessed():
    assert classify_mark(None) == "untagged"                 # no mark at all
    assert classify_mark({"date": "2026-08-01"}) == "untagged"
    assert classify_mark({"vix_close": 30.0}) == "untagged"  # one field only
    assert classify_mark({"spy_vs_20dma_pct": -9.0}) == "untagged"


def test_outcomes_bucket_math():
    marks = {
        "2026-11-10": mark(vix=30.0, spy=-5.0),   # stressed
        "2026-11-11": mark(vix=14.0, spy=1.0),    # calm
    }
    trades = [
        # stressed: +10/share x 10 sh, risk 5 -> +2R, win
        trade("AAA", opened="2026-11-10", exit_price=110.0),
        # stressed: -5/share x 10 sh -> -1R, loss
        trade("BBB", opened="2026-11-10", exit_price=95.0),
        # calm: +5/share x 10 sh -> +1R, win
        trade("CCC", opened="2026-11-11", exit_price=105.0),
        # untagged: entry date has no mark
        trade("DDD", opened="2026-11-12", exit_price=95.0),
    ]
    qty = {f"{t.symbol}-{t.placed_date}": 10 for t in trades}
    out = regime_outcomes(trades, marks, qty)

    s = out["buckets"]["stressed"]
    assert (s["n"], s["n_scored"]) == (2, 2)
    assert s["total_pnl"] == 50.0
    assert s["avg_r"] == 0.5
    assert s["win_rate"] == 0.5

    c = out["buckets"]["calm"]
    assert (c["n"], c["total_pnl"], c["win_rate"]) == (1, 50.0, 1.0)

    u = out["buckets"]["untagged"]
    assert (u["n"], u["total_pnl"]) == (1, -50.0)
    assert out["n_closed"] == 4


def test_missing_evidence_buckets_but_never_scores():
    marks = {"2026-11-10": mark(vix=30.0, spy=-5.0)}
    trades = [
        trade("AAA", exit_price=None),                       # no exit fill
        trade("BBB", exit_price=110.0),                      # no quantity
    ]
    out = regime_outcomes(trades, marks, {})
    s = out["buckets"]["stressed"]
    assert s["n"] == 2 and s["n_scored"] == 0
    assert s["total_pnl"] == 0 and s["avg_r"] is None and s["win_rate"] is None
    gaps = {r["symbol"]: r["gap"] for r in s["unscored"]}
    assert gaps == {"AAA": "no exit-price evidence",
                    "BBB": "no recorded quantity"}


def test_entry_basis_is_fill_when_present_limit_otherwise():
    marks = {"2026-11-10": mark()}
    t = trade("AAA", entry_fill=None, exit_price=105.0)  # falls back to 100.5
    out = regime_outcomes([t], marks, {"AAA-2026-11-10": 10})
    row = out["buckets"]["calm"]["rows"][0]
    assert row["pnl"] == 45.0
