"""Completed-session entry counterfactual (pre-registered 2026-10-05).

Synthetic bars only: the pass bar was committed before the replay touched a
real trade, and these tests pin the rule and the bar so neither can drift.
"""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from autoswing.research import (
    ENTRY_CF_EXCLUDE, ENTRY_CF_MIN_N, LiveTrade, compare_entry_rule,
    completed_session_entry, entry_cf_verdict, find_report,
)

START = "2026-08-10"   # a Monday; bdate_range gives consecutive sessions
D0 = date(2026, 8, 10)


def make_df(rows):
    idx = pd.bdate_range(START, periods=len(rows))
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in rows], index=idx)


def trade(**kw):
    base = dict(symbol="T", entry_date="2026-08-10", entry=100.0,
                stop=95.0, target=110.0, quantity=40)
    base.update(kw)
    return LiveTrade(**base)


# reaction day closes 100; D+1 closes 101 (confirms); D+2 opens 102
CONFIRMED = [(98, 101, 97, 100), (100, 102, 99, 101), (102, 103, 101, 102),
             (102, 120, 101, 118)]


def test_confirmed_enters_at_the_d2_open_with_the_live_stop():
    r = completed_session_entry(trade(), make_df(CONFIRMED), D0)
    assert r["status"] == "entered"
    cf = r["cf_trade"]
    assert cf.entry == 102 and cf.stop == 95.0
    assert cf.entry_date == "2026-08-12"
    assert cf.target == pytest.approx(102 + 2 * 7)


def test_close_below_reaction_close_is_no_trade():
    rows = [(98, 101, 97, 100), (100, 100.5, 98, 99.9), (99, 100, 98, 99)]
    r = completed_session_entry(trade(), make_df(rows), D0)
    assert (r["status"], r["why"]) == ("skipped", "no_confirmation")


def test_equal_close_confirms():
    """'At or above' is literal, pinned so it is not argued later."""
    rows = [(98, 101, 97, 100), (100, 101, 99, 100), (100, 101, 99, 100)]
    assert completed_session_entry(trade(), make_df(rows), D0)["status"] == "entered"


def test_open_at_or_below_the_stop_means_the_setup_broke():
    rows = [(98, 101, 97, 100), (100, 102, 99, 101), (95, 96, 94, 95)]
    r = completed_session_entry(trade(), make_df(rows), D0)
    assert (r["status"], r["why"]) == ("skipped", "setup_broke")


def test_stop_more_than_12pct_away_is_no_trade():
    rows = [(98, 101, 97, 100), (100, 112, 99, 111), (112, 113, 111, 112)]
    r = completed_session_entry(trade(), make_df(rows), D0)   # (112-95)/112 = 15%
    assert (r["status"], r["why"]) == ("skipped", "stop_too_wide")


def test_missing_bars_are_unscorable_not_skips():
    one = make_df([(98, 101, 97, 100)])
    assert completed_session_entry(trade(), one, D0)["status"] == "unscorable"
    two = make_df([(98, 101, 97, 100), (100, 102, 99, 101)])
    assert completed_session_entry(trade(), two, D0)["status"] == "unscorable"
    assert completed_session_entry(trade(), make_df(CONFIRMED),
                                   date(2026, 7, 1))["status"] == "unscorable"


def test_replay_dollars_use_the_live_trades_dollar_risk():
    # live risk = (100-95)*40 = $200. Replay enters 102, stop 95 (risk 7/sh),
    # 2R target 116 hit on the 4th bar -> +2R -> +$400, whatever the share math.
    out = compare_entry_rule([trade()], {"T": make_df(CONFIRMED)},
                             {"T-2026-08-10": {"reaction_date": "2026-08-10"}})
    row = out["rows"][0]
    assert row["cf_r"] == pytest.approx(2.0)
    assert row["cf_pnl"] == pytest.approx(400.0)
    assert row["live_entry_day"] == 0


def test_a_skipped_trade_scores_zero_not_missing():
    rows = [(98, 101, 97, 100), (100, 100.5, 94, 96), (96, 97, 90, 91)]
    out = compare_entry_rule([trade()], {"T": make_df(rows)},
                             {"T-2026-08-10": {"reaction_date": "2026-08-10"}})
    row = out["rows"][0]
    assert row["cf_pnl"] == 0.0 and row["cf_r"] == 0.0
    assert row["live_pnl"] < 0                 # the live day-0 entry stopped out
    assert row["delta_pnl"] == pytest.approx(-row["live_pnl"])


def test_unresolved_reaction_is_listed_never_dropped():
    out = compare_entry_rule([trade()], {"T": make_df(CONFIRMED)},
                             {"T-2026-08-10": {"unresolved": "no earnings report"}})
    assert out["unscorable"][0]["why"] == "no earnings report"
    assert out["verdict_sample"]["n"] == 0


def test_reaction_day_after_live_entry_is_unscorable():
    out = compare_entry_rule([trade()], {"T": make_df(CONFIRMED)},
                             {"T-2026-08-10": {"reaction_date": "2026-08-11"}})
    assert out["rows"][0]["cf_status"] == "unscorable"


# --- the pre-registered verdict -------------------------------------------------

def vrow(symbol, live_pnl, cf_pnl, live_r=None, cf_r=None, status="entered"):
    return {"symbol": symbol, "entry_date": "2026-08-10", "cf_status": status,
            "cf_why": "confirmed", "live_pnl": live_pnl, "cf_pnl": cf_pnl,
            "live_r": live_pnl / 100 if live_r is None else live_r,
            "cf_r": cf_pnl / 100 if cf_r is None else cf_r,
            "delta_pnl": cf_pnl - live_pnl, "live_reason": "stop",
            "cf_reason": "stop"}


def many(n, live, cf):
    return [vrow(f"S{i}", live, cf) for i in range(n)]


def test_too_few_trades_is_insufficient_not_a_near_miss():
    v = entry_cf_verdict(many(ENTRY_CF_MIN_N - 1, -50, 50))
    assert v["verdict"] == "insufficient_sample"


def test_broad_improvement_makes_a_case():
    v = entry_cf_verdict(many(ENTRY_CF_MIN_N, -50, 20))
    assert v["verdict"] == "case_for_hard_rule"


def test_hypothesis_trades_cannot_carry_the_verdict():
    """KMX/SNPS generated the rule, so they are guaranteed to flatter it."""
    rows = many(ENTRY_CF_MIN_N, -50, -50) + [vrow(s, -500, 0) for s in ENTRY_CF_EXCLUDE]
    v = entry_cf_verdict(rows)
    assert v["verdict"] == "no_case"
    assert sorted(v["excluded_from_verdict"]) == sorted(ENTRY_CF_EXCLUDE)
    assert v["all_scorable"]["cf_total_pnl"] > v["all_scorable"]["live_total_pnl"]
    assert v["verdict_sample"]["n"] == ENTRY_CF_MIN_N


def test_gain_resting_on_one_trade_is_no_case():
    rows = many(ENTRY_CF_MIN_N - 1, -50, -60) + [vrow("BIG", -400, 400)]
    v = entry_cf_verdict(rows)
    assert v["dollar_gain"] > 0 and v["dollar_gain_ex_best"] <= 0
    assert v["verdict"] == "no_case"
    assert v["most_helpful_trade"]["symbol"] == "BIG"


def test_dollars_up_but_r_down_is_no_case():
    rows = ([vrow(f"S{i}", -10, -30, live_r=-0.1, cf_r=-1.0)
             for i in range(ENTRY_CF_MIN_N - 2)]
            + [vrow("A", -900, 0, live_r=-1.0, cf_r=0.0),
               vrow("B", -900, 0, live_r=-1.0, cf_r=0.0)])
    v = entry_cf_verdict(rows)
    assert v["dollar_gain"] > 0
    assert v["verdict_sample"]["cf_total_r"] < v["verdict_sample"]["live_total_r"]
    assert v["verdict"] == "no_case"


def test_unscorable_rows_stay_out_of_both_samples_but_are_listed():
    rows = many(ENTRY_CF_MIN_N, -50, 20) + [
        {"symbol": "X", "entry_date": "2026-08-10", "cf_status": "unscorable",
         "cf_why": "no price data"}]
    v = entry_cf_verdict(rows)
    assert v["all_scorable"]["n"] == ENTRY_CF_MIN_N
    assert v["unscorable"][0]["symbol"] == "X"


# --- report lookup ---------------------------------------------------------------

def test_find_report_takes_the_most_recent_on_or_before_entry():
    cal = {date(2026, 8, 7): [SimpleNamespace(symbol="T", report_date="2026-08-07")],
           date(2026, 8, 3): [SimpleNamespace(symbol="T", report_date="2026-08-03")]}
    rep = find_report("t", date(2026, 8, 10), lambda d: cal.get(d, []))
    assert rep.report_date == "2026-08-07"


def test_find_report_returns_none_outside_the_lookback():
    cal = {date(2026, 7, 1): [SimpleNamespace(symbol="T", report_date="2026-07-01")]}
    assert find_report("T", date(2026, 8, 10), lambda d: cal.get(d, [])) is None
