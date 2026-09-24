"""Wheel book tests.

The load-bearing claims, each tested: the realized-vol baseline is the
conservative one; a negative variance premium is rejected; earnings inside
the contract is rejected; the spread can eat a real vol gap; the state
machine refuses illegal transitions; and scoring compares to buy-and-hold
rather than to premium collected.
"""

import math

import pytest

from autoswing.wheel import (
    MIN_N_FOR_VERDICT, PutQuote, advance, bs_put, cycle_pnl, evaluate_put,
    realized_vol, score_book, screen, validate_cycle, vrp_baseline,
)

CAP = 4900.0


def q(**kw):
    base = dict(symbol="TEST", spot=49.0, strike=45.0, bid=1.00, ask=1.10,
                expiry="2026-10-16", dte=22, iv=0.50, open_interest=2000,
                rv120=0.30, rv252=0.30, front_slope=0.0, earnings_date=None)
    base.update(kw)
    return PutQuote(**base)


# --- maths --------------------------------------------------------------------

def test_bs_put_rises_with_vol():
    lo = bs_put(100, 95, 30, 0.20)
    hi = bs_put(100, 95, 30, 0.60)
    assert hi > lo > 0


def test_bs_put_at_expiry_is_intrinsic():
    assert bs_put(90, 100, 0, 0.5) == pytest.approx(10.0)
    assert bs_put(110, 100, 0, 0.5) == pytest.approx(0.0)


def test_realized_vol_none_until_enough_history():
    assert realized_vol([100.0] * 10, 120) is None


def test_realized_vol_matches_hand_calc():
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    rv = realized_vol(closes, 20)
    assert rv is not None and 0.05 < rv < 0.5


def test_vrp_baseline_takes_the_higher_window():
    # The whole point: a calm recent quarter must not become the yardstick.
    assert vrp_baseline(0.20, 0.45) == 0.45
    assert vrp_baseline(0.50, 0.30) == 0.50
    assert vrp_baseline(None, 0.33) == 0.33
    assert vrp_baseline(None, None) is None


# --- screen -------------------------------------------------------------------

def test_clean_quote_passes_and_reports_dollar_edge():
    r = evaluate_put(q(), CAP)
    assert r["passes"], r["reasons"]
    assert r["edge_net_usd"] > 0
    assert r["vrp"] == pytest.approx(0.20)


def test_negative_vrp_rejected():
    r = evaluate_put(q(iv=0.25, rv120=0.40, rv252=0.45), CAP)
    assert not r["passes"]
    assert "negative_or_noise_vrp" in r["reasons"]


def test_vrp_measured_against_long_window_not_short():
    """A name whose recent 120d is calm but whose year is wild must fail."""
    r = evaluate_put(q(iv=0.40, rv120=0.20, rv252=0.55), CAP)
    assert "negative_or_noise_vrp" in r["reasons"]


def test_earnings_inside_contract_rejected():
    r = evaluate_put(q(earnings_date="2026-10-15", expiry="2026-10-16"), CAP)
    assert "earnings_before_expiry" in r["reasons"]


def test_earnings_after_expiry_allowed():
    r = evaluate_put(q(earnings_date="2026-10-28", expiry="2026-10-16"), CAP)
    assert "earnings_before_expiry" not in r["reasons"]


def test_collateral_cap_enforced():
    r = evaluate_put(q(strike=60.0), CAP)
    assert "collateral_over_cap" in r["reasons"]


def test_illiquid_and_wide_spread_rejected():
    r = evaluate_put(q(open_interest=10, bid=0.50, ask=1.50), CAP)
    assert "illiquid_open_interest" in r["reasons"]
    assert "spread_too_wide" in r["reasons"]


def test_unexplained_front_richness_rejected():
    r = evaluate_put(q(front_slope=0.15), CAP)
    assert "unexplained_front_event" in r["reasons"]


def test_mild_front_richness_flags_without_rejecting():
    r = evaluate_put(q(front_slope=0.07), CAP)
    assert r["passes"]
    assert "mild_front_richness" in r["flags"]


def test_wide_spread_can_kill_a_real_vol_gap():
    """A genuine IV-over-RV gap that does not survive crossing the spread
    must be rejected -- this is the finding that sank most of the first
    screen's candidates."""
    # A 0.5-vol-point gap is real but worth ~$1.16/contract; crossing half a
    # 12.5% spread costs $2.00. The spread gate itself passes -- it is the
    # dollar edge that fails, which is the only honest test.
    r = evaluate_put(q(iv=0.305, rv120=0.30, rv252=0.30, bid=0.30, ask=0.34), CAP)
    assert r["spread_pct"] < 0.22
    assert r["edge_net_usd"] < 0
    assert "no_edge_after_spread" in r["reasons"]


def test_missing_baseline_is_rejected_not_assumed():
    r = evaluate_put(q(rv120=None, rv252=None), CAP)
    assert "no_realized_vol_baseline" in r["reasons"]
    assert r["edge_net_usd"] is None


def test_all_rejection_reasons_reported_not_just_first():
    r = evaluate_put(q(strike=60.0, open_interest=5, iv=0.10,
                       rv120=0.5, rv252=0.5), CAP)
    assert len(r["reasons"]) >= 3


def test_screen_ranks_by_edge_and_tallies_rejections():
    out = screen([q(symbol="A"), q(symbol="B", iv=0.70),
                  q(symbol="C", iv=0.20)], CAP)
    assert out["evaluated"] == 3
    assert [c["symbol"] for c in out["candidates"]] == ["B", "A"]
    assert out["rejection_tally"]["negative_or_noise_vrp"] == 1


def test_static_yield_is_reported_but_not_the_ranking_key():
    """A thin-premium contract with a big vol gap should outrank a fat-premium
    one with none."""
    fat = q(symbol="FAT", bid=3.0, ask=3.2, iv=0.31, rv120=0.30, rv252=0.30)
    thin = q(symbol="THIN", bid=1.0, ask=1.05, iv=0.60, rv120=0.30, rv252=0.30)
    out = screen([fat, thin], CAP)
    assert out["candidates"][0]["symbol"] == "THIN"


# --- state machine ------------------------------------------------------------

def base_cycle(**kw):
    c = {"id": "T-2026-09-24-45p", "symbol": "TEST", "status": "csp_open",
         "opened": "2026-09-24", "strike": 45.0, "expiry": "2026-10-16",
         "premium_received": 1.00, "spot_at_open": 49.0, "contracts": 1,
         "shares": 0, "call_strike": None, "call_expiry": None,
         "closed": None, "exit_price": None, "legs": [], "note": "x"}
    c.update(kw)
    return c


def test_put_expires_worthless_is_terminal():
    c = advance(base_cycle(), "expire_worthless", "2026-10-16", price=47.0)
    assert c["status"] == "csp_expired"
    assert c["closed"] == "2026-10-16"


def test_assignment_then_call_then_called_away():
    c = advance(base_cycle(), "assign", "2026-10-16")
    assert c["status"] == "assigned" and c["shares"] == 100
    c = advance(c, "sell_call", "2026-10-19", premium=0.80,
                strike=46.0, expiry="2026-11-20")
    assert c["status"] == "cc_open"
    assert c["premium_received"] == pytest.approx(1.80)
    c = advance(c, "call_away", "2026-11-20")
    assert c["status"] == "called_away" and c["exit_price"] == 46.0


def test_call_can_expire_and_be_resold():
    c = advance(base_cycle(), "assign", "2026-10-16")
    c = advance(c, "sell_call", "2026-10-19", premium=0.80, strike=46.0,
                expiry="2026-11-20")
    c = advance(c, "expire_worthless", "2026-11-20")
    assert c["status"] == "cc_expired"
    c = advance(c, "sell_call", "2026-11-23", premium=0.70, strike=46.0,
                expiry="2026-12-18")
    assert c["premium_received"] == pytest.approx(2.50)


def test_illegal_transitions_raise():
    with pytest.raises(ValueError):
        advance(base_cycle(), "call_away", "2026-10-16")
    with pytest.raises(ValueError):
        advance(base_cycle(status="csp_expired"), "assign", "2026-10-16")


def test_sell_call_requires_its_terms():
    c = advance(base_cycle(), "assign", "2026-10-16")
    with pytest.raises(ValueError):
        advance(c, "sell_call", "2026-10-19", premium=0.8)


def test_every_transition_is_journalled_as_a_leg():
    c = advance(base_cycle(), "assign", "2026-10-16")
    c = advance(c, "sell_call", "2026-10-19", premium=0.8, strike=46.0,
                expiry="2026-11-20")
    assert [l["event"] for l in c["legs"]] == ["assign", "sell_call"]


# --- scoring ------------------------------------------------------------------

def test_expired_put_pnl_is_premium_only():
    c = advance(base_cycle(), "expire_worthless", "2026-10-16", price=49.0)
    r = cycle_pnl(c)
    assert r["premium_usd"] == pytest.approx(100.0)
    assert r["stock_pnl_usd"] == 0.0
    assert r["total_pnl_usd"] == pytest.approx(100.0)


def test_wheel_loses_to_hold_when_the_stock_rallies():
    """The core correction: capping upside at the premium is a real cost, and
    the scoreboard has to show it."""
    c = advance(base_cycle(), "expire_worthless", "2026-10-16", price=60.0)
    r = cycle_pnl(c)
    assert r["total_pnl_usd"] == pytest.approx(100.0)
    assert r["hold_pnl_usd"] > r["total_pnl_usd"]
    assert r["vs_hold_usd"] < 0


def test_wheel_beats_hold_on_a_mild_decline():
    c = advance(base_cycle(), "expire_worthless", "2026-10-16", price=46.0)
    r = cycle_pnl(c)
    assert r["vs_hold_usd"] > 0


def test_open_cycle_needs_a_mark():
    c = advance(base_cycle(), "assign", "2026-10-16")
    assert cycle_pnl(c).get("pending") is True
    assert cycle_pnl(c, mark=40.0)["stock_pnl_usd"] == pytest.approx(-500.0)


def test_called_away_counts_stock_gain_to_the_call_strike():
    c = advance(base_cycle(), "assign", "2026-10-16")
    c = advance(c, "sell_call", "2026-10-19", premium=0.80, strike=46.0,
                expiry="2026-11-20")
    c = advance(c, "call_away", "2026-11-20")
    r = cycle_pnl(c)
    assert r["stock_pnl_usd"] == pytest.approx(100.0)   # (46-45)*100
    assert r["premium_usd"] == pytest.approx(180.0)
    assert r["total_pnl_usd"] == pytest.approx(280.0)


def test_score_book_refuses_a_verdict_on_a_short_sample():
    c = advance(base_cycle(), "expire_worthless", "2026-10-16", price=47.0)
    out = score_book([c])
    assert out["n_closed"] == 1
    assert "too few cycles" in out["verdict"]
    assert out["min_n_for_verdict"] == MIN_N_FOR_VERDICT


def test_score_book_reports_vs_hold_and_worst_cycle():
    good = advance(base_cycle(id="g"), "expire_worthless", "2026-10-16", price=46.0)
    bad = advance(base_cycle(id="b"), "assign", "2026-10-16")
    bad = advance(bad, "sell_call", "2026-10-19", premium=0.5, strike=46.0,
                  expiry="2026-11-20")
    bad = advance(bad, "call_away", "2026-11-20")
    out = score_book([good, bad])
    assert out["n_closed"] == 2
    assert "vs_hold_usd" in out and "worst_cycle_usd" in out
    assert out["premium_usd"] > 0


def test_pending_marks_are_counted_not_silently_dropped():
    openc = advance(base_cycle(id="o"), "assign", "2026-10-16")
    out = score_book([openc], marks={})
    assert out["n_pending_mark"] == 1
    assert out["n_closed"] == 0


# --- validation ---------------------------------------------------------------

def test_validate_requires_benchmark_entry_and_rationale():
    errs = validate_cycle({"symbol": "X", "opened": "2026-09-24",
                           "expiry": "2026-10-16", "strike": 45,
                           "premium_received": 1.0})
    assert any("spot_at_open" in e for e in errs)
    assert any("note required" in e for e in errs)


def test_validate_accepts_a_complete_payload():
    assert validate_cycle({"symbol": "X", "opened": "2026-09-24",
                           "expiry": "2026-10-16", "strike": 45,
                           "premium_received": 1.0, "spot_at_open": 49.0,
                           "note": "would own it here"}) == []


# --- open option legs are liabilities, not income -----------------------------

def test_open_put_without_option_mark_is_pending_not_profitable():
    """Booking the premium the day it is sold would show the book in profit
    for the whole life of every contract."""
    r = cycle_pnl(base_cycle(), mark=49.0)
    assert r["pending"] is True
    assert "liability" in r["reason"]


def test_open_put_nets_the_buyback_cost():
    # Sold for 1.00, now costs 1.60 to close: that is a 60 dollar loss.
    r = cycle_pnl(base_cycle(), mark=45.5, option_mark=1.60)
    assert r["pending"] is False
    assert r["premium_usd"] == pytest.approx(-60.0)


def test_open_put_that_decayed_shows_partial_gain():
    r = cycle_pnl(base_cycle(), mark=49.5, option_mark=0.40)
    assert r["premium_usd"] == pytest.approx(60.0)
    assert r["total_pnl_usd"] == pytest.approx(60.0)


def test_assigned_cycle_needs_no_option_mark():
    """Once assigned there is no live short option -- only shares."""
    c = advance(base_cycle(), "assign", "2026-10-16")
    r = cycle_pnl(c, mark=43.0)
    assert r["pending"] is False
    assert r["stock_pnl_usd"] == pytest.approx(-200.0)


def test_score_book_routes_option_marks_by_cycle_id():
    a = base_cycle(id="a", symbol="AA")
    b = base_cycle(id="b", symbol="AA", strike=40.0)
    out = score_book([a, b], marks={"AA": 46.0},
                     option_marks={"a": 0.50, "b": 0.10})
    assert out["n_pending_mark"] == 0
    prem = {r["id"]: r["premium_usd"] for r in out["rows"]}
    assert prem["a"] == pytest.approx(50.0)
    assert prem["b"] == pytest.approx(90.0)
