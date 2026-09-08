"""Candidate pipeline: recent reporters -> reaction metrics -> hard floors.

Produces the JSON the brain reasons over. Floors mirror the risk gate so
the brain rarely proposes something the gate would bounce; the gate still
re-checks everything (defense in depth).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .earnings import Report, recent_reporters
from .prices import Reaction, fetch_history, reaction_metrics

# A surprise this large, answered by a market move this far the OTHER way,
# means the surprise number and the market are grading different quarters:
# the feed's consensus basis (GAAP vs street-adjusted) or vintage is suspect,
# or the story isn't EPS at all. GTLB 2026-09-02 is the worked example: the
# feed graded a street-adjusted +33% beat as a -85.7% GAAP miss while the
# stock gapped +22% on 2x volume — only a manual news check caught it.
CONTRADICTION_SURPRISE_PCT = 25.0


def build_candidate(report: Report, reaction: Reaction | None, floors: dict,
                    has_prices: bool = True) -> dict:
    flags = list(report.quality_flags)
    if (
        report.surprise_pct is not None
        and reaction is not None
        and abs(report.surprise_pct) >= CONTRADICTION_SURPRISE_PCT
        and abs(reaction.move_pct) >= floors["min_reaction_move_pct"]
        and (report.surprise_pct > 0) != (reaction.move_pct > 0)
    ):
        flags.append("reaction_contradicts_surprise")
    c = {
        "symbol": report.symbol,
        "company": report.company,
        "report_date": report.report_date,
        "timing": report.timing,
        "eps_actual": report.eps_actual,
        "eps_forecast": report.eps_forecast,
        "surprise_pct": report.surprise_pct,
        "num_estimates": report.num_estimates,
        # Never a rejection — labels why the headline surprise may mislead
        # (thin coverage, tiny denominator, one-off items, a reaction that
        # contradicts the graded surprise). See earnings.quality_flags.
        "quality_flags": flags,
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


# Insider window: wide enough to catch the post-print open-window buys AND
# accumulation in the weeks before the report; transaction dates ride along
# in the ledger so the regression can slice it finer.
FORM4_LOOKBACK_DAYS = 90


def insider_enrichment(symbol: str, today: date,
                       state_dir: Path | None = None) -> dict:
    """Form 4 cluster-buying summary for ONE passing candidate
    (research instrument #8; green-lit 2026-09-08, measurement-only).

    Never raises and never returns nothing: every failure comes back as
    {"unavailable": reason} so an EDGAR outage is distinguishable from
    "no insider bought". Results are cached per (symbol, day) in
    state/edgar/ because scan-candidates also runs in the hourly
    healthchecks — only the first scan of a day pays the fetch.
    """
    from .. import edgar

    if state_dir is None:
        from ..config import PROJECT_ROOT
        state_dir = PROJECT_ROOT / "state" / "edgar"
    cache_file = state_dir / "form4-cache.json"
    key = symbol.upper()
    cache: dict = {}
    try:
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
    except Exception:
        cache = {}
    hit = cache.get(key)
    if hit and hit.get("fetched") == today.isoformat():
        return hit["summary"]

    try:
        cik = edgar.cik_for_ticker(key, state_dir / "tickers.json")
        if cik is None:
            summary = {"unavailable": "no unambiguous CIK for ticker"}
        else:
            buys, meta = edgar.form4_purchases(
                cik, today - timedelta(days=FORM4_LOOKBACK_DAYS))
            summary = edgar.insider_summary(buys, meta)
            summary["lookback_days"] = FORM4_LOOKBACK_DAYS
    except Exception as e:
        # Not cached: a transient EDGAR failure should retry next run.
        return {"unavailable": f"{type(e).__name__}: {e}"}

    cache[key] = {"fetched": today.isoformat(), "summary": summary}
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2))
        tmp.replace(cache_file)
    except Exception:
        pass  # a failed cache write costs a refetch, never the result
    return summary


def scan(risk_config: dict, days_back: int = 3, min_move_pct: float = 3.0,
         today: date | None = None, insider_enrich=None) -> dict:
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
    # Measurement-only field on PASSING candidates (the population whose
    # drift we can later join). Floors and rejects never read it.
    enrich = insider_enrich or insider_enrichment
    for c in passing:
        c["insider_buying"] = enrich(c["symbol"], today or date.today())
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
