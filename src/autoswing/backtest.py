"""Historical backtest of the mechanical PEAD skeleton.

Replays the calendar-filter -> reaction-filter -> bracket-simulation chain
over Nasdaq's historical earnings calendar (frozen records, verified
2026-08-11 — see docs/backtest-feasibility.md). Reuses the live pipeline
wherever it exists: reaction_metrics() for reaction-day inference and
mark_position() (the conservative stop-first fill model) for simulation,
at the wide ledger's standardized notional.

Three caveats stated with every result readout (never quote results as
expected live performance):
- reaction day is INFERRED from price action (historical rows carry no
  BMO/AMC timing);
- possible source-side survivorship (Nasdaq may regenerate old calendar
  pages from its current symbol table);
- the brain's judgment layer (news checks, "is this beat clean") is not
  replayed — this measures the mechanical skeleton only.

Lookahead honesty: with timing unknown, reaction-day inference compares
sessions D and D+1, so the decision is only available after D+1's close.
Entry is therefore the OPEN of the session after D+1, even when the
inferred reaction day was D.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from .data.earnings import Report, fetch_calendar_rows, parse_calendar_rows
from .data.prices import fetch_window, reaction_metrics
from .shadow import WIDE_NOTIONAL, ShadowPosition, mark_position

DEFAULTS = {
    "min_surprise_pct": 5.0,     # calendar stage: EPS beat size
    "min_estimates": 3,          # thin-coverage floor (playbook: beware tiny counts)
    "min_move_pct": 5.0,         # reaction stage: confirming move
    "min_volume_ratio": 2.0,     # reaction stage: confirming volume
    "max_stop_distance_pct": 8.0,  # playbook: stop farther than this = too hot
    "max_pullback_pct": 3.0,     # drift intact between reaction close and entry eve
    "max_hold_days": 15,         # same time-box the live book uses
    # skeleton: enter the open after D+1 (the original model).
    # delay:    always enter the open two sessions after the REACTION day,
    #           same 3% pullback tolerance (descriptive only).
    # confirm:  as delay, but the session after the reaction day must CLOSE
    #           at or above the reaction-day close (the completed-session rule).
    "entry_mode": "skeleton",
    # long:  beat + up-reaction, stop at the reaction-day LOW (the live book).
    # short: miss + down-reaction, stop at the reaction-day HIGH -- the exact
    #        mirror, research only (see the short-side registration below).
    "side": "long",
}

# -- completed-session filter: out-of-sample check ------------------------------
# The live replay (2026-10-05, n=33) passed its pre-registered bar thinly. This
# asks the same question of 2023-25, which the rule has never seen. Committed
# BEFORE the confirm variant was run.
#
# PRE-REGISTERED BAR, confirm vs skeleton on identical data and fill model:
#   supports_hard_rule   : avg R higher AND total R higher overall AND total R
#                          higher in at least 2 of the 3 calendar years.
#   better_per_trade_only: avg R higher, but total R is not (overall, or in
#                          fewer than 2 years) -- better selection that makes
#                          less money in total is a sizing question, not a
#                          case for a hard entry rule.
#   no_support           : avg R not higher.
# The "delay" variant is reported for interpretation only and cannot change
# the verdict. Standing caveats: inferred reaction days, survivorship,
# mechanical skeleton only, stop = reaction low capped at 8% (the live replay
# used the live trade's stop and a 12% ceiling).
CONFIRM_REGISTERED = "2026-10-05"
CONFIRM_BAR = ("confirm vs skeleton: avg R higher AND total R higher overall "
               "AND total R higher in >= 2 of 3 years")


def confirm_verdict(skeleton: dict, confirm: dict) -> dict:
    """skeleton/confirm are run_backtest results."""
    so, co = skeleton["overall"], confirm["overall"]
    years = sorted(set(skeleton["by_year"]) | set(confirm["by_year"]))
    year_wins = [y for y in years
                 if confirm["by_year"].get(y, {}).get("total_r", 0.0)
                 > skeleton["by_year"].get(y, {}).get("total_r", 0.0)]
    out = {"bar": CONFIRM_BAR, "registered": CONFIRM_REGISTERED,
           "skeleton": {k: so.get(k) for k in ("n", "hit_rate", "avg_r", "total_r")},
           "confirm": {k: co.get(k) for k in ("n", "hit_rate", "avg_r", "total_r")},
           "years_total_r_higher": year_wins, "years": years}
    if not co.get("n") or not so.get("n"):
        out["verdict"] = "undeterminable"
        return out
    avg_up = co["avg_r"] > so["avg_r"]
    tot_up = co["total_r"] > so["total_r"]
    if not avg_up:
        out["verdict"] = "no_support"
    elif tot_up and len(year_wins) >= 2:
        out["verdict"] = "supports_hard_rule"
    else:
        out["verdict"] = "better_per_trade_only"
    return out

# Price window around a report: 20 sessions of pre-report ADV needs ~45
# calendar days back; entry ~D+2 plus a 15-trading-day time-box needs ~45 forward.
PRICE_LOOKBACK_DAYS = 45
PRICE_LOOKFORWARD_DAYS = 45

CALENDAR_PACING_S = 0.35


# -- calendar cache -----------------------------------------------------------

def cached_calendar_day(
    day: date, cache_dir: Path, session: requests.Session | None = None
) -> list[Report]:
    """Calendar rows for one day, cached forever (the record is frozen).
    Raw rows are cached rather than parsed Reports so parser fixes apply on
    reread."""
    cache = cache_dir / f"{day.isoformat()}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        rows = fetch_calendar_rows(day, session)
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(cache)
        time.sleep(CALENDAR_PACING_S)
    return parse_calendar_rows(rows, day)


# -- delay variant: test on unseen years ------------------------------------------
# The 2023-25 run was registered for "confirm"; "delay" was descriptive there
# and happened to score best (+161.3R on n=1342 vs skeleton +87.9R on 1612).
# That is a hypothesis generated FROM those years, so they cannot test it.
# 2021-22 had never been run when this was committed.
#
# What delay changes, precisely: nothing for reactions on the report day
# (skeleton and delay are identical there). For reactions on the day AFTER
# the report -- after-close reporters -- the skeleton buys the next open with
# no completed session behind it; delay waits one session and skips only if
# that session closed more than 3% below the reaction close.
#
# PRE-REGISTERED BAR, delay vs skeleton on 2021-01-01..2022-12-31:
#   supports             : avg R higher AND total R higher overall AND total R
#                          higher in at least 3 of the 4 half-years.
#   better_per_trade_only: avg R higher, total R not (overall or < 3 halves).
#   no_support           : avg R not higher.
# A pass is a case for the OWNER to consider one specific rule -- after an
# after-close report, wait one full session past the reaction day -- and
# says nothing about same-day (before-open) reporters, where the two
# variants do not differ. Never an automatic change. "confirm" is re-run
# alongside for comparison only.
DELAY_REGISTERED = "2026-10-05"
DELAY_BAR = ("delay vs skeleton on unseen 2021-22: avg R higher AND total R "
             "higher overall AND total R higher in >= 3 of 4 half-years")


def half_year_totals(trades: list[dict]) -> dict:
    out: dict[str, float] = {}
    for t in trades:
        y, m = t["entry_date"][:4], int(t["entry_date"][5:7])
        key = f"{y}H{1 if m <= 6 else 2}"
        out[key] = out.get(key, 0.0) + t["r_multiple"]
    return {k: round(v, 2) for k, v in sorted(out.items())}


def delay_verdict(skeleton: dict, delay: dict) -> dict:
    so, do = skeleton["overall"], delay["overall"]
    sh = half_year_totals(skeleton.get("trades", []))
    dh = half_year_totals(delay.get("trades", []))
    halves = sorted(set(sh) | set(dh))
    wins = [h for h in halves if dh.get(h, 0.0) > sh.get(h, 0.0)]
    out = {"bar": DELAY_BAR, "registered": DELAY_REGISTERED,
           "skeleton": {k: so.get(k) for k in ("n", "hit_rate", "avg_r", "total_r")},
           "delay": {k: do.get(k) for k in ("n", "hit_rate", "avg_r", "total_r")},
           "half_years": {h: {"skeleton": sh.get(h, 0.0), "delay": dh.get(h, 0.0)}
                          for h in halves},
           "half_years_total_r_higher": wins}
    if not so.get("n") or not do.get("n"):
        out["verdict"] = "undeterminable"
        return out
    need = 3 if len(halves) >= 4 else len(halves)
    out["half_years_needed"] = need
    if not do["avg_r"] > so["avg_r"]:
        out["verdict"] = "no_support"
    elif do["total_r"] > so["total_r"] and len(wins) >= need:
        out["verdict"] = "supports"
    else:
        out["verdict"] = "better_per_trade_only"
    return out


# -- short side: does the drift exist on misses? ----------------------------------
# Owner question 2026-11-10: can the book go long-short. The live gate forbids
# shorts and the project's worst incident was an accidental one, so the first
# step is the free one -- replay the MIRROR of the skeleton on history the
# short side has never been run on. Committed BEFORE the first short run.
#
# The mirror, precisely: EPS miss of >= 5% (actual < forecast, >= 3
# estimates), reaction of <= -5% on >= 2x volume, same liquidity and price
# floors (price floor on the reaction close, i.e. AFTER the drop), enter the
# open after D+1, skip if a close before entry bounced more than 3% above the
# reaction close, stop = reaction-day HIGH (skip if entry is at/above it or
# the stop is more than 8% away), target = 2R below entry, 15-day time-box.
# Fills use mark_position on a price-negated frame, so the fill model
# (stop-first on ambiguous bars) is the long book's by construction.
#
# A short pays to borrow and the long side does not, so the bar is judged
# NET of an assumed borrow fee: SHORT_BORROW_ANNUAL_PCT a year on the entry
# notional for each calendar day held. The true fee is unknowable per name
# historically (general collateral is well under 1%, hard-to-borrow names can
# be 20%+); 5% is a registered placeholder, and 0% / 25% are reported for
# interpretation only.
#
# PRE-REGISTERED BAR, short skeleton on 2021-01-01..2025-12-31, net of borrow:
#   supports_short_shadow_book: avg net R > 0 AND total net R > 0 in at
#                          least 3 of the 5 calendar years (by entry date)
#                          AND total net R > 0 with trades entered in 2022
#                          removed (the one down year for the index in the
#                          sample: VOO -18% vs +18..+29% in the other four
#                          -- there any short is paid by the market, not by
#                          the drift).
#   positive_but_fragile : avg net R > 0, but fewer than 3 positive years or
#                          nothing left without 2022.
#   no_support           : avg net R <= 0.
#   undeterminable       : no trades, or any price cohort failed to download
#                          (rerun; a throttled feed must not shrink the
#                          sample silently).
#   unregistered_range   : any other date range -- descriptive, not a verdict.
# A pass is a case for the OWNER to consider a zero-capital short SHADOW
# book and nothing more: never a live short, never a gate change. What the
# replay cannot see, stated with every readout: whether shares could be
# borrowed at all, the short-sale restriction after a 10% drop (it covers
# the next session too, which is often the entry session), squeeze gaps
# through the stop, and names since delisted (missing from the free price
# source in both directions: bankruptcies and buyouts).
SHORT_REGISTERED = "2026-11-10"
SHORT_SAMPLE = ("2021-01-01", "2025-12-31")
SHORT_BORROW_ANNUAL_PCT = 5.0
SHORT_BEAR_YEAR = "2022"
SHORT_YEARS_NEEDED = 3
SHORT_BAR = ("short skeleton 2021-25, net of 5%/yr borrow: avg R > 0 AND "
             "total R > 0 in >= 3 of 5 years AND total R > 0 without 2022")
SHORT_CAVEATS = [
    "borrow availability is not modelled -- a name that could not be "
    "borrowed still counts as a trade",
    "borrow fee is a flat registered assumption, not the per-name rate",
    "short-sale restriction after a 10% drop is not modelled (it often "
    "covers the entry session)",
    "stops fill at the stop price; a squeeze gap through the stop is "
    "only caught when the OPEN is already past it at entry",
    "names since delisted are missing from the free price source",
]


def borrow_cost_r(trade: dict, annual_pct: float) -> float:
    """Borrow fee for one short trade, in R. Calendar days, because the fee
    accrues every day a settled short is held (weekends included); a
    same-day round trip pays nothing."""
    days = (date.fromisoformat(trade["exit_date"])
            - date.fromisoformat(trade["entry_date"])).days
    risk = trade["stop"] - trade["entry_price"]
    if days <= 0 or risk <= 0:
        return 0.0
    return (annual_pct / 100) * (days / 365) * trade["entry_price"] / risk


def net_of_borrow(trades: list[dict], annual_pct: float) -> list[dict]:
    """Copies of short trades with r_multiple and pnl net of the borrow fee.
    Dollars use the same share count the simulation did."""
    out = []
    for t in trades:
        cost_r = borrow_cost_r(t, annual_pct)
        qty = max(1, int(WIDE_NOTIONAL // t["entry_price"]))
        cost_usd = cost_r * (t["stop"] - t["entry_price"]) * qty
        out.append({**t,
                    "r_multiple": round(t["r_multiple"] - cost_r, 3),
                    "pnl": round(t["pnl"] - cost_usd, 2),
                    "borrow_cost_r": round(cost_r, 4)})
    return out


def short_verdict(result: dict) -> dict:
    """result is a run_backtest result with side == 'short'."""
    trades = result.get("trades", [])
    net = net_of_borrow(trades, SHORT_BORROW_ANNUAL_PCT)
    years = [str(y) for y in range(int(SHORT_SAMPLE[0][:4]),
                                   int(SHORT_SAMPLE[1][:4]) + 1)]
    by_year_net = aggregate_by_year(net)
    year_totals = {y: by_year_net.get(y, {}).get("total_r", 0.0) for y in years}
    positive_years = [y for y in years if year_totals[y] > 0]
    gross, net_overall = aggregate(trades), aggregate(net)
    # Years are ENTRY years (aggregate_by_year). A report from the last days
    # of 2025 enters in January 2026: it counts in the overall and the
    # without-2022 totals, and in none of the five year buckets.
    ex_bear = round(net_overall.get("total_r", 0.0)
                    - year_totals[SHORT_BEAR_YEAR], 2)
    keys = ("n", "hit_rate", "avg_r", "total_r")
    funnel = result.get("funnel", {})
    out = {
        "bar": SHORT_BAR, "registered": SHORT_REGISTERED,
        "sample": list(SHORT_SAMPLE),
        "borrow_annual_pct": SHORT_BORROW_ANNUAL_PCT,
        "gross": {k: gross.get(k) for k in keys},
        "net": {k: net_overall.get(k) for k in keys},
        "net_total_r_by_year": year_totals,
        "positive_years": positive_years,
        "years_needed": SHORT_YEARS_NEEDED,
        "net_total_r_without_" + SHORT_BEAR_YEAR: ex_bear,
        "price_fetch_failed": funnel.get("price_fetch_failed", 0),
        "price_data_unavailable": funnel.get("skips", {}).get(
            "price_data_unavailable", 0),
        "caveats": SHORT_CAVEATS,
    }
    rng = result.get("range", {})
    if result.get("params", {}).get("side") != "short":
        out["verdict"] = "undeterminable"
        out["reason"] = "not a short-side run"
    elif (rng.get("start"), rng.get("end")) != SHORT_SAMPLE:
        out["verdict"] = "unregistered_range"
    elif out["price_fetch_failed"]:
        out["verdict"] = "undeterminable"
        out["reason"] = ("price download failed for part of the sample; "
                         "rerun (failed cohorts are not cached)")
    elif not net_overall.get("n"):
        out["verdict"] = "undeterminable"
        out["reason"] = "no trades"
    elif not net_overall["avg_r"] > 0:
        out["verdict"] = "no_support"
    elif len(positive_years) >= SHORT_YEARS_NEEDED and ex_bear > 0:
        out["verdict"] = "supports_short_shadow_book"
    else:
        out["verdict"] = "positive_but_fragile"
    return out


# -- filters ------------------------------------------------------------------

def calendar_prefilter(reports: list[Report], params: dict) -> list[Report]:
    """Rows worth pricing: complete triplet, clean positive beat, not
    thinly covered. Mirrors the playbook's 'real surprise' bar as far as
    it can be made mechanical. side == 'short' takes the mirror: a clean
    miss of the same size."""
    short = params.get("side", "long") == "short"
    out = []
    for r in reports:
        if r.eps_actual is None or r.eps_forecast is None or r.surprise_pct is None:
            continue
        if short:
            if r.surprise_pct > -params["min_surprise_pct"]:
                continue
            if r.eps_actual >= r.eps_forecast:
                continue  # inconsistent row; the triplet must agree with itself
        else:
            if r.surprise_pct < params["min_surprise_pct"]:
                continue
            if r.eps_actual <= r.eps_forecast:
                continue  # inconsistent row; the triplet must agree with itself
        if (r.num_estimates or 0) < params["min_estimates"]:
            continue
        out.append(r)
    return out


# -- prices -------------------------------------------------------------------

# Fetched alongside a cohort that came back completely empty, to tell "these
# names are gone from the source" from "the source is down or throttling us".
PRICE_CANARY = "SPY"
# The live scans read the same free source. A cold run is ~1,300 cohort
# downloads, so it is paced, and it stops asking altogether once the source
# has failed FETCH_FAILURE_LIMIT cohorts in a row (the rest of the run is
# then cache-only and reports itself incomplete).
PRICE_PACING_S = 1.0
FETCH_FAILURE_LIMIT = 3


def cohort_prices(
    day: date, symbols: list[str], cache_dir: Path,
    failed: list[str] | None = None, offline: bool = False,
) -> dict[str, pd.DataFrame]:
    """OHLCV windows for one report-day cohort, cached per symbol. A symbol
    with no cache file after a fetch attempt gets an empty marker so reruns
    don't re-request known-missing names.

    The marker is permanent, so it is only written when the source was
    demonstrably up: if NOTHING came back for the uncached names, a canary
    fetch decides. Canary also empty = outage or throttling -- no markers,
    and the names are appended to `failed` so the run can say it is
    incomplete instead of quietly judging a smaller sample. offline=True
    skips the network entirely: uncached names go straight to `failed`."""
    day_dir = cache_dir / day.isoformat()
    out: dict[str, pd.DataFrame] = {}
    missing = []
    for sym in symbols:
        f = day_dir / f"{sym}.csv"
        if f.exists():
            if f.stat().st_size > 0:
                out[sym] = pd.read_csv(f, index_col=0, parse_dates=True)
        else:
            missing.append(sym)
    if missing and offline:
        if failed is not None:
            failed.extend(missing)
        return out
    if missing:
        fetched = fetch_window(
            missing,
            day - timedelta(days=PRICE_LOOKBACK_DAYS),
            day + timedelta(days=PRICE_LOOKFORWARD_DAYS),
        )
        time.sleep(PRICE_PACING_S)
        if not fetched and PRICE_CANARY not in fetch_window(
                [PRICE_CANARY],
                day - timedelta(days=PRICE_LOOKBACK_DAYS),
                day + timedelta(days=PRICE_LOOKFORWARD_DAYS)):
            if failed is not None:
                failed.extend(missing)
            return out
        day_dir.mkdir(parents=True, exist_ok=True)
        for sym in missing:
            f = day_dir / f"{sym}.csv"
            if sym in fetched:
                fetched[sym].to_csv(f)
                out[sym] = fetched[sym]
            else:
                f.touch()  # empty marker: known-missing
    return out


# -- simulation ---------------------------------------------------------------

def _negated(df: pd.DataFrame) -> pd.DataFrame:
    """Price-negated copy for marking a short as a long: -High is the new
    Low and -Low the new High, so bar ranges stay well-formed."""
    out = df.copy()
    out["Open"], out["Close"] = -df["Open"], -df["Close"]
    out["High"], out["Low"] = -df["Low"], -df["High"]
    return out


def simulate_candidate(report: Report, df: pd.DataFrame, params: dict) -> dict:
    """One candidate through the mechanical skeleton. Returns a record with
    outcome: 'trade', a named skip, or 'open_at_data_end'."""
    sym = report.symbol
    rdate = date.fromisoformat(report.report_date)
    rec = {"symbol": sym, "report_date": report.report_date,
           "surprise_pct": report.surprise_pct}

    # reaction_metrics' drift/last_close fields read the end of df (future
    # bars), but none of the fields used below do — they stop at the
    # inferred reaction day.
    reaction = reaction_metrics(sym, df, rdate, "unknown")
    if reaction is None:
        return {**rec, "outcome": "skip", "reason": "no_reaction_data"}
    short = params.get("side", "long") == "short"
    sign = -1 if short else 1
    # sign * move is "how far it moved in the trade's direction".
    if sign * reaction.move_pct <= 0 or sign * reaction.move_pct < params["min_move_pct"]:
        return {**rec, "outcome": "skip", "reason": "weak_reaction"}
    if reaction.volume_ratio < params["min_volume_ratio"]:
        return {**rec, "outcome": "skip", "reason": "weak_volume"}
    if reaction.adv_dollar_20d < params["min_avg_dollar_volume"]:
        return {**rec, "outcome": "skip", "reason": "illiquid"}

    dates = [d.date() for d in df.index]
    reaction_idx = dates.index(date.fromisoformat(reaction.reaction_date))
    r_close = float(df["Close"].iloc[reaction_idx])
    if r_close < params["min_price"]:
        return {**rec, "outcome": "skip", "reason": "min_price"}

    # Decision needs both D and D+1 closed (see module docstring): entry is
    # the open of the session after D+1, which is >= reaction_idx + 1.
    after = [i for i, d in enumerate(dates) if d > rdate]
    mode = params.get("entry_mode", "skeleton")
    # delay/confirm anchor on the REACTION day, which is never earlier than
    # the skeleton's entry, so the lookahead rule above still holds.
    entry_idx = after[0] + 1 if mode == "skeleton" else reaction_idx + 2
    if entry_idx >= len(df):
        return {**rec, "outcome": "skip", "reason": "no_entry_session"}

    if mode == "confirm":
        if sign * (float(df["Close"].iloc[reaction_idx + 1]) - r_close) < 0:
            return {**rec, "outcome": "skip", "reason": "no_confirmation"}
    else:
        # Drift intact between reaction close and entry eve: no close gave
        # back more than the tolerance (long: fell below; short: bounced above).
        eve_closes = df["Close"].iloc[reaction_idx + 1:entry_idx]
        tol = params["max_pullback_pct"] / 100
        if len(eve_closes) and (
                float(eve_closes.max()) > r_close * (1 + tol) if short
                else float(eve_closes.min()) < r_close * (1 - tol)):
            return {**rec, "outcome": "skip", "reason": "drift_broken"}

    entry_price = float(df["Open"].iloc[entry_idx])
    stop = float(df["High" if short else "Low"].iloc[reaction_idx])
    risk = sign * (entry_price - stop)      # per-share distance to the stop
    if risk <= 0:
        return {**rec, "outcome": "skip", "reason":
                "gapped_above_stop" if short else "gapped_below_stop"}
    stop_distance_pct = 100 * risk / entry_price
    if stop_distance_pct > params["max_stop_distance_pct"]:
        return {**rec, "outcome": "skip", "reason": "stop_geometry"}

    target = entry_price + sign * 2 * risk
    quantity = max(1, int(WIDE_NOTIONAL // entry_price))
    # A short is marked as a long on the price-negated frame: the negation
    # swaps High/Low and flips every comparison, so mark_position's rules
    # (stop-first on ambiguous bars, close-price time-box) apply unchanged
    # and there is no second fill model to drift from the first.
    mark_df = _negated(df) if short else df
    pos = ShadowPosition(
        symbol=sym, strategy="backtest",
        opened=dates[entry_idx].isoformat(),
        entry_price=sign * entry_price, quantity=quantity,
        stop_loss=sign * stop, take_profit=sign * target,
    )
    # Entry is AT the open, so the entry bar's full range is post-entry.
    event = mark_position(pos, mark_df, dates[-1], params["max_hold_days"],
                          entry_at_open=True)
    if event is None:
        return {**rec, "outcome": "open_at_data_end"}
    event["exit_price"] = sign * event["exit_price"]   # pnl is already right

    r_multiple = sign * (event["exit_price"] - entry_price) / risk
    return {
        **rec, "outcome": "trade",
        "entry_date": pos.opened, "entry_price": round(entry_price, 4),
        "stop": round(stop, 4), "target": round(target, 4),
        "exit_date": event["closed"], "exit_price": event["exit_price"],
        "exit_reason": event["reason"], "days_held": event["days_held"],
        "pnl": event["pnl"], "r_multiple": round(r_multiple, 3),
        "reaction_move_pct": reaction.move_pct,
        "volume_ratio": reaction.volume_ratio,
    }


# -- aggregation --------------------------------------------------------------

def aggregate(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    rs = [t["r_multiple"] for t in trades]
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "n": len(trades),
        "hit_rate": round(len(wins) / len(trades), 3),
        "avg_r": round(sum(rs) / len(rs), 3),
        "total_r": round(sum(rs), 2),
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "avg_days_held": round(sum(t["days_held"] for t in trades) / len(trades), 1),
        "exit_reasons": reasons,
    }


def aggregate_by_year(trades: list[dict]) -> dict:
    years = sorted({t["entry_date"][:4] for t in trades})
    return {y: aggregate([t for t in trades if t["entry_date"].startswith(y)])
            for y in years}


# -- driver -------------------------------------------------------------------

def run_backtest(
    start: date, end: date, risk_config: dict, cache_root: Path,
    overrides: dict | None = None, progress=None,
) -> dict:
    params = {
        **DEFAULTS,
        "min_avg_dollar_volume": float(risk_config["min_avg_dollar_volume"]),
        "min_price": float(risk_config.get("min_price", 5.0)),
        **(overrides or {}),
    }
    session = requests.Session()
    trades, skips, open_at_end = [], {}, 0
    scanned = prefiltered = priced = 0
    fetch_failed: list[str] = []
    failed_streak, offline_from = 0, None

    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        reports = cached_calendar_day(day, cache_root / "calendar", session)
        scanned += len(reports)
        # one row per symbol per day; dedup keeps the first
        cohort: dict[str, Report] = {}
        for r in calendar_prefilter(reports, params):
            cohort.setdefault(r.symbol, r)
        prefiltered += len(cohort)
        if cohort:
            failed_today: list[str] = []
            prices = cohort_prices(day, sorted(cohort), cache_root / "prices",
                                   failed_today, offline=offline_from is not None)
            fetch_failed.extend(failed_today)
            failed_streak = failed_streak + 1 if failed_today else 0
            if failed_streak >= FETCH_FAILURE_LIMIT and offline_from is None:
                offline_from = day.isoformat()
            priced += len(prices)
            for sym, report in cohort.items():
                df = prices.get(sym)
                if df is None:
                    # an outage is not "this name has no data": keep them apart
                    why = ("price_fetch_failed" if sym in failed_today
                           else "price_data_unavailable")
                    skips[why] = skips.get(why, 0) + 1
                    continue
                result = simulate_candidate(report, df, params)
                if result["outcome"] == "trade":
                    trades.append(result)
                elif result["outcome"] == "open_at_data_end":
                    open_at_end += 1
                else:
                    skips[result["reason"]] = skips.get(result["reason"], 0) + 1
        if progress:
            progress(day, len(trades))
        day += timedelta(days=1)

    return {
        "params": params,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "funnel": {"calendar_rows": scanned, "prefiltered": prefiltered,
                   "with_prices": priced, "trades": len(trades),
                   "open_at_data_end": open_at_end,
                   "price_fetch_failed": len(fetch_failed),
                   "fetch_stopped_after": offline_from, "skips": skips},
        "overall": aggregate(trades),
        "by_year": aggregate_by_year(trades),
        "trades": trades,
        "caveats": [
            "reaction day inferred from price action (no BMO/AMC timing in "
            "historical rows)",
            "possible source-side survivorship in Nasdaq's historical calendar",
            "mechanical skeleton only — the brain's judgment layer is not replayed",
        ] + (SHORT_CAVEATS if params.get("side") == "short" else []),
    }
