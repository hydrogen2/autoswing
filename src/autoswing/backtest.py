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
}

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


# -- filters ------------------------------------------------------------------

def calendar_prefilter(reports: list[Report], params: dict) -> list[Report]:
    """Rows worth pricing: complete triplet, clean positive beat, not
    thinly covered. Mirrors the playbook's 'real surprise' bar as far as
    it can be made mechanical."""
    out = []
    for r in reports:
        if r.eps_actual is None or r.eps_forecast is None or r.surprise_pct is None:
            continue
        if r.surprise_pct < params["min_surprise_pct"]:
            continue
        if r.eps_actual <= r.eps_forecast:
            continue  # inconsistent row; the triplet must agree with itself
        if (r.num_estimates or 0) < params["min_estimates"]:
            continue
        out.append(r)
    return out


# -- prices -------------------------------------------------------------------

def cohort_prices(
    day: date, symbols: list[str], cache_dir: Path
) -> dict[str, pd.DataFrame]:
    """OHLCV windows for one report-day cohort, cached per symbol. A symbol
    with no cache file after a fetch attempt gets an empty marker so reruns
    don't re-request known-missing names."""
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
    if missing:
        fetched = fetch_window(
            missing,
            day - timedelta(days=PRICE_LOOKBACK_DAYS),
            day + timedelta(days=PRICE_LOOKFORWARD_DAYS),
        )
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
    if reaction.move_pct <= 0 or reaction.move_pct < params["min_move_pct"]:
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
    entry_idx = after[0] + 1
    if entry_idx >= len(df):
        return {**rec, "outcome": "skip", "reason": "no_entry_session"}

    # Drift intact between reaction close and entry eve.
    eve_closes = df["Close"].iloc[reaction_idx + 1:entry_idx]
    if len(eve_closes) and float(eve_closes.min()) < r_close * (1 - params["max_pullback_pct"] / 100):
        return {**rec, "outcome": "skip", "reason": "drift_broken"}

    entry_price = float(df["Open"].iloc[entry_idx])
    stop = float(df["Low"].iloc[reaction_idx])
    if entry_price <= stop:
        return {**rec, "outcome": "skip", "reason": "gapped_below_stop"}
    stop_distance_pct = 100 * (entry_price - stop) / entry_price
    if stop_distance_pct > params["max_stop_distance_pct"]:
        return {**rec, "outcome": "skip", "reason": "stop_geometry"}

    target = entry_price + 2 * (entry_price - stop)
    quantity = max(1, int(WIDE_NOTIONAL // entry_price))
    pos = ShadowPosition(
        symbol=sym, strategy="backtest",
        opened=dates[entry_idx].isoformat(),
        entry_price=entry_price, quantity=quantity,
        stop_loss=stop, take_profit=target,
    )
    event = mark_position(pos, df, dates[-1], params["max_hold_days"])
    if event is None:
        return {**rec, "outcome": "open_at_data_end"}

    r_multiple = (event["exit_price"] - entry_price) / (entry_price - stop)
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
            prices = cohort_prices(day, sorted(cohort), cache_root / "prices")
            priced += len(prices)
            for sym, report in cohort.items():
                df = prices.get(sym)
                if df is None:
                    skips["price_data_unavailable"] = skips.get("price_data_unavailable", 0) + 1
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
                   "open_at_data_end": open_at_end, "skips": skips},
        "overall": aggregate(trades),
        "by_year": aggregate_by_year(trades),
        "trades": trades,
        "caveats": [
            "reaction day inferred from price action (no BMO/AMC timing in "
            "historical rows)",
            "possible source-side survivorship in Nasdaq's historical calendar",
            "mechanical skeleton only — the brain's judgment layer is not replayed",
        ],
    }
