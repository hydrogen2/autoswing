"""Pre-registered wide-ledger verdict (owner-approved 2026-10-14)."""

from types import SimpleNamespace

import pytest

from autoswing.shadow import WIDE_NOTIONAL, WIDE_VERDICT_N, wide_verdict


def wrow(sym, opened, entry, exit_, pnl, alpha=0.0):
    return {"symbol": sym, "opened": opened, "entry_price": entry,
            "exit_price": exit_, "pnl": pnl, "alpha_pct": alpha}


def live(entry, stop, exit_):
    return SimpleNamespace(entry_limit=entry, stop_loss=stop, exit_price=exit_)


def build(n, wide_exit, live_exit, entry=100.0, stop=95.0):
    """n paired trades, wide and live sharing the bracket."""
    rows, lv = [], {}
    for i in range(n):
        sym, d = f"S{i}", f"2026-09-{(i % 28) + 1:02d}"
        qty = int(WIDE_NOTIONAL // entry)
        rows.append(wrow(sym, d, entry, wide_exit, round((wide_exit - entry) * qty, 2)))
        lv[f"{sym}-{d}"] = live(entry, stop, live_exit)
    return rows, lv


def test_pending_below_40_reports_progress_only():
    rows, lv = build(23, 104.0, 104.0)
    v = wide_verdict(rows, lv)
    assert v["verdict"] == "pending" and v["remaining"] == WIDE_VERDICT_N - 23
    assert v["paired"]["n"] == 23


def test_unpaired_rows_are_reported_but_never_judged():
    rows, lv = build(WIDE_VERDICT_N, 104.0, 104.0)
    rows[0] = wrow("ONLYWIDE", "2026-09-01", 50.0, 70.0, 2000.0)   # no live match
    v = wide_verdict(rows, lv)
    assert v["unpaired"]["n"] == 1 and v["unpaired"]["total_pnl"] == 2000.0
    assert v["paired"]["n"] == WIDE_VERDICT_N - 1


def test_identical_outcomes_are_no_case():
    rows, lv = build(WIDE_VERDICT_N, 104.0, 104.0)
    v = wide_verdict(rows, lv)
    assert v["verdict"] == "no_case"
    assert v["dollar_gain"] == pytest.approx(0.0) and v["avg_r_gain"] == pytest.approx(0.0)


def test_live_dollars_are_rescaled_to_wide_notional():
    """Sizing must not decide it: a live trade at any share count scores at
    the wide book's standardized notional."""
    rows, lv = build(WIDE_VERDICT_N, 104.0, 104.0)
    v = wide_verdict(rows, lv)
    assert v["paired"]["live_total_pnl_rescaled"] == pytest.approx(
        v["paired"]["wide_total_pnl"])


def test_broad_edge_earns_a_discussion():
    rows, lv = build(WIDE_VERDICT_N, 105.0, 104.0)      # wide fills a point better
    v = wide_verdict(rows, lv)
    assert v["verdict"] == "playbook_discussion"
    assert v["dollar_gain_ex_best"] > 0 and v["avg_r_gain_ex_best"] > 0


def test_edge_resting_on_one_trade_is_no_case():
    rows, lv = build(WIDE_VERDICT_N, 104.0, 104.5)      # wide slightly worse...
    rows[5] = wrow("S5", "2026-09-06", 100.0, 140.0, 2000.0)   # ...except one monster
    v = wide_verdict(rows, lv)
    assert v["dollar_gain"] > 0
    assert v["verdict"] == "no_case"
    assert v["dropped_best"]["symbol"] == "S5"


def test_verdict_is_frozen_at_the_first_40_closes():
    rows, lv = build(WIDE_VERDICT_N, 103.0, 104.0)      # wide loses on the first 40
    later, lv2 = build(10, 130.0, 104.0)
    for r in later:
        r["symbol"] = "L" + r["symbol"]
    lv.update({f"L{k}": v for k, v in lv2.items()})
    v = wide_verdict(rows + later, lv)
    assert v["verdict"] == "no_case" and v["sample_n"] == WIDE_VERDICT_N


def test_too_few_pairs_is_insufficient_not_a_near_miss():
    rows, lv = build(WIDE_VERDICT_N, 105.0, 104.0)
    lv = {k: v for i, (k, v) in enumerate(lv.items()) if i < 10}
    assert wide_verdict(rows, lv)["verdict"] == "insufficient_pairs"


def test_missing_live_exit_counts_as_unpaired():
    rows, lv = build(WIDE_VERDICT_N, 104.0, 104.0)
    lv["S0-2026-09-01"] = live(100.0, 95.0, None)
    v = wide_verdict(rows, lv)
    assert v["unpaired"]["n"] == 1
