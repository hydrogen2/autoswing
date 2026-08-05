"""Candidate pipeline: recent reporters -> reaction metrics -> hard floors.

Produces the JSON the brain reasons over. Floors mirror the risk gate so
the brain rarely proposes something the gate would bounce; the gate still
re-checks everything (defense in depth).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from .earnings import Report, recent_reporters
from .prices import Reaction, fetch_history, reaction_metrics


def build_candidate(report: Report, reaction: Reaction | None, floors: dict,
                    has_prices: bool = True) -> dict:
    c = {
        "symbol": report.symbol,
        "company": report.company,
        "report_date": report.report_date,
        "timing": report.timing,
        "eps_actual": report.eps_actual,
        "eps_forecast": report.eps_forecast,
        "surprise_pct": report.surprise_pct,
        "num_estimates": report.num_estimates,
        "market_cap": report.market_cap,
        "reaction": asdict(reaction) if reaction else None,
        "rejects": [],
    }
    if reaction is None:
        # A failed price download and a report that hasn't traded yet both
        # yield reaction=None, but they mean opposite things: the first is our
        # bug to retry, the second is "re-check tomorrow". Labelling both
        # no_reaction_data_yet let good candidates vanish silently (08-05).
        c["rejects"].append(
            "no_reaction_data_yet" if has_prices else "price_data_unavailable"
        )
        return c
    if reaction.adv_dollar_20d < floors["min_avg_dollar_volume"]:
        c["rejects"].append(
            f"illiquid: ADV ${reaction.adv_dollar_20d:,.0f} < ${floors['min_avg_dollar_volume']:,.0f}"
        )
    if reaction.last_close < floors["min_price"]:
        c["rejects"].append(f"price ${reaction.last_close} < ${floors['min_price']}")
    if reaction.move_pct <= 0:
        c["rejects"].append(
            f"negative reaction {reaction.move_pct}% (long-only strategy)"
        )
    elif abs(reaction.move_pct) < floors["min_reaction_move_pct"]:
        c["rejects"].append(
            f"reaction {reaction.move_pct}% too small (<{floors['min_reaction_move_pct']}%)"
        )
    return c


def scan(risk_config: dict, days_back: int = 3, min_move_pct: float = 3.0,
         today: date | None = None) -> dict:
    floors = {
        "min_avg_dollar_volume": float(risk_config["min_avg_dollar_volume"]),
        "min_price": float(risk_config.get("min_price", 5.0)),
        "min_reaction_move_pct": min_move_pct,
    }
    reports = recent_reporters(days_back, today=today)
    # One row per symbol: keep the most recent report.
    by_symbol: dict[str, Report] = {}
    for r in sorted(reports, key=lambda r: r.report_date):
        by_symbol[r.symbol] = r

    history = fetch_history(sorted(by_symbol))
    candidates = []
    for sym, report in by_symbol.items():
        df = history.get(sym)
        reaction = (
            reaction_metrics(sym, df, date.fromisoformat(report.report_date), report.timing)
            if df is not None else None
        )
        candidates.append(build_candidate(report, reaction, floors, has_prices=df is not None))

    passing = [c for c in candidates if not c["rejects"]]
    passing.sort(key=lambda c: abs(c["reaction"]["move_pct"]), reverse=True)
    # Surfaced so a shrinking candidate list is attributable to a data outage
    # rather than read as "nothing qualified today".
    no_prices = sorted(s for s in by_symbol if s not in history)
    return {
        "scanned": len(candidates),
        "passing": len(passing),
        "price_data_missing": len(no_prices),
        "price_data_missing_symbols": no_prices,
        "candidates": passing,
        "rejected": [
            {"symbol": c["symbol"], "rejects": c["rejects"]}
            for c in candidates if c["rejects"]
        ],
    }
